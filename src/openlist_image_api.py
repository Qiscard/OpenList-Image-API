#!/usr/bin/env python3
"""Secure, dependency-free random-image API for OpenList."""

from __future__ import annotations

import argparse
import gzip
import hmac
import io
import json
import logging
import os
import random
import secrets
import socket
import threading
import time
import zipfile
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen

DEFAULT_CONFIG: dict[str, Any] = {
    "listen_host": "0.0.0.0",
    "listen_port": 8790,
    "openlist_api_url": "http://127.0.0.1:5244",
    "openlist_token_file": "/etc/openlist-image-api/openlist.token",
    "state_dir": "/var/lib/openlist-image-api",
    "directories": [],
    "extensions": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp"],
    "view_layout": "single",
    "delivery": "preview",
    "caption_mode": "path",
    "directory_display_enabled": True,
    "directory_display_depth": 0,
    "announcement_enabled": False,
    "announcement_title": "网站公告",
    "announcement_content": "",
    "announcement_required_seconds": 0,
    "announcement_version": 0,
    "maintenance_enabled": False,
    "grid_gap": 12,
    "grid_scale": 150,
    "url_cache_size": 1000,
    "url_cache_ttl_seconds": 1800,
    "admin_token_file": "/etc/openlist-image-api/admin.token",
}
ALLOWED_LAYOUTS = {"single", "grid", "waterfall"}
ALLOWED_DELIVERY = {"preview", "download"}
ALLOWED_CAPTION_MODES = {"path", "name", "hidden"}
MAX_REQUEST_BODY = 64 * 1024
URL_RESOLVE_WORKERS = 12
DEVICE_PREFERENCE_DEFAULTS: dict[str, Any] = {
    "view_layout": DEFAULT_CONFIG["view_layout"],
    "grid_gap": DEFAULT_CONFIG["grid_gap"],
}


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
    if config["listen_host"] not in {"127.0.0.1", "0.0.0.0"}:
        raise ValueError("listen_host must be 127.0.0.1 or 0.0.0.0")
    if not isinstance(config["listen_port"], int) or not 1024 <= config["listen_port"] <= 65535:
        raise ValueError("listen_port must be between 1024 and 65535")
    if not is_loopback_openlist_url(config["openlist_api_url"]):
        raise ValueError("openlist_api_url must point to a local HTTP OpenList service")
    config["directories"] = normalize_directories(config["directories"])
    if config["view_layout"] not in ALLOWED_LAYOUTS:
        raise ValueError("invalid view_layout")
    if config["delivery"] not in ALLOWED_DELIVERY:
        raise ValueError("invalid delivery")
    if config["caption_mode"] not in ALLOWED_CAPTION_MODES:
        raise ValueError("invalid caption_mode")
    if not isinstance(config["directory_display_enabled"], bool):
        raise ValueError("directory_display_enabled must be a boolean")
    if not isinstance(config["directory_display_depth"], int) or not 0 <= config["directory_display_depth"] <= 64:
        raise ValueError("directory_display_depth must be between 0 and 64")
    if not isinstance(config["announcement_enabled"], bool):
        raise ValueError("announcement_enabled must be a boolean")
    for key, limit in (("announcement_title", 120), ("announcement_content", 4000)):
        if not isinstance(config[key], str):
            raise ValueError(f"{key} must be a string")
        config[key] = config[key].strip()
        if len(config[key]) > limit:
            raise ValueError(f"{key} is too long")
    if not isinstance(config["announcement_required_seconds"], int) or not 0 <= config["announcement_required_seconds"] <= 3600:
        raise ValueError("announcement_required_seconds must be between 0 and 3600")
    if not isinstance(config["announcement_version"], int) or config["announcement_version"] < 0:
        raise ValueError("announcement_version must be a non-negative integer")
    if not isinstance(config["maintenance_enabled"], bool):
        raise ValueError("maintenance_enabled must be a boolean")
    if not isinstance(config["grid_gap"], int) or not 0 <= config["grid_gap"] <= 48:
        raise ValueError("grid_gap must be between 0 and 48")
    if not isinstance(config["grid_scale"], int) or not 75 <= config["grid_scale"] <= 200:
        raise ValueError("grid_scale must be between 75 and 200")
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


def admin_token_from_headers(headers: Any) -> str | None:
    token = headers.get("X-OpenList-Admin-Token") or headers.get("X-Admin-Token")
    return token if isinstance(token, str) and token else None


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
                return {"images": [], "directories": [], "directory_index": {}, "generated_at": 0, "directory_generated_at": 0, "errors": []}
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
    started_at = time.time()
    client = OpenListClient(config)
    extensions = set(config["extensions"])
    queue: deque[str] = deque(config["directories"])
    visited: set[str] = set()
    images: list[dict[str, Any]] = []
    directory_index: dict[str, list[dict[str, str]]] = {}
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
        children: list[dict[str, str]] = []
        for entry in entries:
            name = str(entry.get("name") or "")
            if not name or "/" in name or "\\" in name:
                continue
            path = join_virtual_path(current, name)
            if entry.get("is_dir"):
                children.append({"name": name, "path": path})
                queue.append(path)
            elif Path(name).suffix.lower() in extensions:
                try:
                    size = max(0, int(entry.get("size") or 0))
                except (TypeError, ValueError):
                    size = 0
                images.append({"path": path, "size": size})
        directory_index[current] = sorted(children, key=lambda item: item["name"].casefold())

    index = {
        "version": 2,
        "generated_at": int(time.time()),
        "directory_generated_at": int(time.time()),
        "build_duration_seconds": round(time.time() - started_at, 2),
        "directories": config["directories"],
        "directory_index": directory_index,
        "directory_count": len(visited),
        "image_count": len(images),
        "errors": errors,
        "images": images,
    }
    repository.save(index)
    return index


def build_directory_index(config: dict[str, Any]) -> dict[str, Any]:
    client = OpenListClient(config)
    queue: deque[str] = deque(config["directories"])
    visited: set[str] = set()
    directory_index: dict[str, list[dict[str, str]]] = {}
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
        children: list[dict[str, str]] = []
        for entry in entries:
            name = str(entry.get("name") or "")
            if not entry.get("is_dir") or not name or "/" in name or "\\" in name:
                continue
            path = join_virtual_path(current, name)
            children.append({"name": name, "path": path})
            queue.append(path)
        directory_index[current] = sorted(children, key=lambda item: item["name"].casefold())
    return {
        "directory_index": directory_index,
        "directory_count": len(visited),
        "directory_generated_at": int(time.time()),
        "directory_errors": errors,
    }


class UrlCache:
    def __init__(self, max_size: int, ttl_seconds: int):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._lock = threading.Lock()
        self._resolve_locks = [threading.Lock() for _ in range(64)]
        self.hits = 0
        self.misses = 0

    def _cached_url(self, path: str) -> str | None:
        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(path)
            if cached and now - cached[0] < self.ttl_seconds:
                self.hits += 1
                self._entries.move_to_end(path)
                return cached[1]
            if cached:
                self._entries.pop(path, None)
        return None

    def invalidate(self, path: str) -> None:
        with self._lock:
            self._entries.pop(path, None)

    def resolve(self, path: str, client: OpenListClient, refresh: bool = False) -> str:
        if refresh:
            self.invalidate(path)
        else:
            cached_url = self._cached_url(path)
            if cached_url is not None:
                return cached_url
        resolve_lock = self._resolve_locks[hash(path) % len(self._resolve_locks)]
        with resolve_lock:
            if not refresh:
                cached_url = self._cached_url(path)
                if cached_url is not None:
                    return cached_url
            with self._lock:
                self.misses += 1
            url = client.resolve_file(path)
            if self.max_size:
                with self._lock:
                    self._entries[path] = (time.monotonic(), url)
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
        self.url_executor = ThreadPoolExecutor(max_workers=URL_RESOLVE_WORKERS, thread_name_prefix="openlist-url")
        self.config_lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.refreshing = False
        self.last_refresh_error = ""

    def reload_config(self) -> dict[str, Any]:
        previous_config = self.config
        self.config = load_config(self.config_path)
        self.repository = IndexRepository(Path(self.config["state_dir"]))
        cache_changed = any(
            previous_config[key] != self.config[key]
            for key in ("url_cache_size", "url_cache_ttl_seconds", "openlist_api_url", "openlist_token_file")
        )
        if cache_changed:
            self.cache = UrlCache(self.config["url_cache_size"], self.config["url_cache_ttl_seconds"])
        return self.config

    def visitor_config(self) -> dict[str, Any]:
        config = DEVICE_PREFERENCE_DEFAULTS.copy()
        config["caption_mode"] = self.config["caption_mode"]
        config["directory_display_enabled"] = self.config["directory_display_enabled"]
        config["directory_display_depth"] = self.config["directory_display_depth"]
        config["announcement"] = {
            "enabled": self.config["announcement_enabled"],
            "title": self.config["announcement_title"] if self.config["announcement_enabled"] else "",
            "content": self.config["announcement_content"] if self.config["announcement_enabled"] else "",
            "required_seconds": self.config["announcement_required_seconds"] if self.config["announcement_enabled"] else 0,
            "version": self.config["announcement_version"],
        }
        config["maintenance_enabled"] = self.config["maintenance_enabled"]
        return config

    def public_config(self) -> dict[str, Any]:
        return self.visitor_config()

    def admin_config(self) -> dict[str, Any]:
        return {
            "directories": self.config["directories"],
            "caption_mode": self.config["caption_mode"],
            "directory_display_enabled": self.config["directory_display_enabled"],
            "directory_display_depth": self.config["directory_display_depth"],
            "announcement_enabled": self.config["announcement_enabled"],
            "announcement_title": self.config["announcement_title"],
            "announcement_content": self.config["announcement_content"],
            "announcement_required_seconds": self.config["announcement_required_seconds"],
            "announcement_version": self.config["announcement_version"],
            "maintenance_enabled": self.config["maintenance_enabled"],
        }

    def update_admin_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "directories",
            "caption_mode",
            "directory_display_enabled",
            "directory_display_depth",
            "announcement_enabled",
            "announcement_title",
            "announcement_content",
            "announcement_required_seconds",
            "maintenance_enabled",
        }
        if set(payload) - allowed:
            raise ValueError("unsupported configuration field")
        with self.config_lock:
            candidate = self.config.copy()
            candidate.update(payload)
            announcement_fields = {"announcement_enabled", "announcement_title", "announcement_content", "announcement_required_seconds"}
            if any(candidate[key] != self.config[key] for key in announcement_fields):
                candidate["announcement_version"] = self.config["announcement_version"] + 1
            validated = validate_config(candidate)
            atomic_write_json(self.config_path, validated)
            self.reload_config()
            return self.admin_config()

    def create_config_backup(self) -> bytes:
        backup = {
            "schema_version": 3,
            "exported_at": int(time.time()),
            "config": {
                "listen_port": self.config["listen_port"],
                "openlist_api_url": self.config["openlist_api_url"],
                "directories": self.config["directories"],
                "caption_mode": self.config["caption_mode"],
                "directory_display_enabled": self.config["directory_display_enabled"],
                "directory_display_depth": self.config["directory_display_depth"],
                "announcement_enabled": self.config["announcement_enabled"],
                "announcement_title": self.config["announcement_title"],
                "announcement_content": self.config["announcement_content"],
                "announcement_required_seconds": self.config["announcement_required_seconds"],
                "announcement_version": self.config["announcement_version"],
                "maintenance_enabled": self.config["maintenance_enabled"],
                "url_cache_size": self.config["url_cache_size"],
                "url_cache_ttl_seconds": self.config["url_cache_ttl_seconds"],
            },
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("openlist-image-api-config.json", json.dumps(backup, ensure_ascii=False, indent=2) + "\n")
        return output.getvalue()

    def restore_config_backup(self, body: bytes) -> dict[str, Any]:
        try:
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                names = archive.namelist()
                if names != ["openlist-image-api-config.json"]:
                    raise ValueError("backup archive has invalid contents")
                info = archive.getinfo(names[0])
                if info.file_size > MAX_REQUEST_BODY:
                    raise ValueError("backup configuration is too large")
                backup = json.loads(archive.read(names[0]))
        except (OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError) as error:
            raise ValueError(f"invalid configuration backup: {error}") from error
        if not isinstance(backup, dict) or not isinstance(backup.get("config"), dict):
            raise ValueError("backup configuration is invalid")
        allowed = {
            "directories",
            "caption_mode",
            "directory_display_enabled",
            "directory_display_depth",
            "announcement_enabled",
            "announcement_title",
            "announcement_content",
            "announcement_required_seconds",
            "maintenance_enabled",
        }
        payload = {key: value for key, value in backup["config"].items() if key in allowed}
        if not payload:
            raise ValueError("backup has no restorable configuration")
        return self.update_admin_config(payload)

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

    def start_directory_refresh(self) -> bool:
        if not self.refresh_lock.acquire(blocking=False):
            return False
        self.refreshing = True

        def worker() -> None:
            try:
                index = self.repository.load()
                index.update(build_directory_index(self.config))
                self.repository.save(index)
                self.last_refresh_error = ""
            except Exception as error:  # logged and visible through status
                logging.exception("Directory cache refresh failed")
                self.last_refresh_error = str(error)
            finally:
                self.refreshing = False
                self.refresh_lock.release()

        threading.Thread(target=worker, name="openlist-directory-refresh", daemon=True).start()
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
            "directory_generated_at": int(index.get("directory_generated_at") or 0),
            "last_build_duration_seconds": float(index.get("build_duration_seconds") or 0),
            "refreshing": self.refreshing,
            "last_refresh_error": self.last_refresh_error,
            "cache": self.cache.status(),
            **self.public_config(),
        }

    def list_directories(self, path: str) -> list[dict[str, str]]:
        directory = normalize_directory(path)
        index = self.repository.load()
        directory_index = index.get("directory_index")
        if isinstance(directory_index, dict):
            entries = directory_index.get(directory)
            if isinstance(entries, list):
                return [item for item in entries if isinstance(item, dict) and isinstance(item.get("name"), str) and isinstance(item.get("path"), str)]
        if directory == "/":
            roots = [{"name": item.rsplit("/", 1)[-1] or "/", "path": item} for item in self.config["directories"]]
            return sorted(roots, key=lambda item: item["name"].casefold())
        return []

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

    def resolve_images(self, images: list[dict[str, Any]], refresh: bool = False) -> list[dict[str, Any]]:
        client = OpenListClient(self.config)

        def resolve(image: dict[str, Any]) -> dict[str, Any]:
            path = str(image["path"])
            url = self.cache.resolve(path, client, refresh=True) if refresh else self.cache.resolve(path, client)
            return {"path": path, "size": int(image.get("size") or 0), "url": url}

        return list(self.url_executor.map(resolve, images))


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


def attachment_disposition(filename: str) -> str:
    ascii_name = "".join(character if character.isascii() and (character.isalnum() or character in ".-_") else "_" for character in filename)
    if not any(character.isalnum() for character in ascii_name):
        ascii_name = "download"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename, safe='')}"


def gallery_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>图库</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#10131a;color:#e7edf7;font:15px system-ui,sans-serif}header{position:sticky;z-index:2;top:0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:14px 18px;background:#10131af2;border-bottom:1px solid #293040}button,.button{background:#4b8cff;border:0;border-radius:7px;color:#fff;padding:9px 13px;cursor:pointer;text-decoration:none}button:disabled{opacity:.45;cursor:not-allowed}.meta{color:#a9b7cd;font-size:13px}.spacer{flex:1}.gallery{padding:18px}.gallery.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));grid-auto-flow:dense;gap:var(--grid-gap,12px);align-items:start}.gallery.grid .card.wide{grid-column:span 2}.gallery.waterfall{display:flex;align-items:flex-start;gap:var(--grid-gap,12px)}.waterfall-column{display:flex;min-width:0;flex:1;flex-direction:column;gap:var(--grid-gap,12px)}.gallery.single{display:grid;min-height:calc(100vh - 80px);place-items:center}.gallery.single .card{max-width:min(96vw,1280px)}.card{background:#171c27;border:1px solid #293040;border-radius:10px;overflow:hidden}.preview-button{display:block;width:100%;padding:0;border:0;border-radius:0;background:transparent}.card img{width:100%;display:block;max-height:82vh;object-fit:contain;background:#080a0f}.gallery.grid .card img{max-height:none}.card footer{display:flex;gap:10px;align-items:center;justify-content:space-between;padding:9px 11px}.caption{margin:0;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.download{color:#b7d1ff;white-space:nowrap}.hidden{display:none!important}a{color:inherit}#empty{padding:40px;text-align:center;color:#a9b7cd}dialog{width:min(96vw,1500px);height:min(94vh,1000px);padding:0;border:1px solid #3a455b;border-radius:12px;background:#10131a;color:#e7edf7}dialog::backdrop{background:#000c}.lightbox-head,.lightbox-foot{display:flex;gap:12px;align-items:center;padding:10px 12px}.lightbox-head{justify-content:space-between}.lightbox-controls{display:flex;gap:8px}.lightbox-stage{height:calc(94vh - 126px);overflow:auto;background:#080a0f;display:grid;place-items:center}.lightbox-image{display:block;width:100%;height:100%;object-fit:contain;transform-origin:center;transition:transform .15s ease}.lightbox-meta{min-width:0;display:grid;gap:4px}.lightbox-caption,.lightbox-directory{margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.lightbox-directory{color:#a9b7cd;font-size:13px}.modal-backdrop{position:fixed;z-index:4;inset:0;background:#0008}#announcement-backdrop{position:fixed!important;z-index:1000!important;inset:0!important;background:#10131a!important;opacity:1!important;pointer-events:auto!important;animation:backdrop-in .25s ease both}.announcement{z-index:1001!important;pointer-events:auto!important}body.announcement-open{overflow:hidden}body.announcement-open>header,body.announcement-open>main,body.announcement-open>#maintenance{pointer-events:none!important;user-select:none}body.announcement-open>#announcement-backdrop,body.announcement-open>#announcement{display:block!important;visibility:visible!important}.preferences{position:fixed;z-index:5;top:50%;left:50%;width:min(92vw,430px);height:auto;padding:20px;border:1px solid #3a455b;border-radius:12px;background:#10131a;color:#e7edf7;transform:translate(-50%,-50%);box-shadow:0 1.5rem 2.2rem rgba(0,0,0,.28)}.preferences h2{margin-top:0}.preferences label{display:grid;gap:7px;margin:14px 0}.preferences select,.preferences input{padding:9px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff}.preferences-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}.announcement{position:fixed;z-index:5;top:50%;left:50%;width:min(94vw,680px);height:auto;padding:0;border:0;border-radius:22px;background:linear-gradient(145deg,#fffdf8 0%,#fff 54%,#fff0e3 100%);color:#282828;box-shadow:0 1.5rem 3rem rgba(0,0,0,.38);overflow:hidden;transform:translate(-50%,-50%);animation:announcement-in .32s cubic-bezier(.2,.8,.2,1) both}.announcement.is-closing{animation:announcement-out .22s ease-in both}@keyframes backdrop-in{from{opacity:0}to{opacity:1}}@keyframes announcement-in{from{opacity:0;transform:translate(-50%,-46%) scale(.96)}to{opacity:1;transform:translate(-50%,-50%) scale(1)}}@keyframes announcement-out{from{opacity:1;transform:translate(-50%,-50%) scale(1)}to{opacity:0;transform:translate(-50%,-46%) scale(.97)}}.announcement-main{padding:26px 30px 12px}.announcement-title{position:relative;z-index:0;display:inline-block;margin:0 0 18px;font-size:21px}.announcement-title::after{content:'';position:absolute;z-index:-1;right:-3px;bottom:2px;left:-3px;height:14px;border-radius:4px;background:#fbeecd;transform:skewX(-15deg)}.announcement-content{margin:0;line-height:1.7}.announcement-content h1,.announcement-content h2,.announcement-content h3,.announcement-content h4{margin:16px 0 8px}.announcement-content p{margin:8px 0}.announcement-content code{padding:2px 5px;border-radius:4px;background:#f3f5f7}.announcement-content pre{overflow:auto;padding:12px;border-radius:8px;background:#f3f5f7}.announcement-content pre code{padding:0}.announcement-content a{color:#b63813}.announcement-footer{padding:12px 30px 28px;text-align:center;background:linear-gradient(170deg,#fff 0%,#fff 38%,#fbeecd 100%)}.announcement-actions{display:flex;justify-content:center;gap:10px;flex-wrap:wrap}.announcement-footer button{border-radius:50px;background:linear-gradient(to right,#ff711f,#e50914);box-shadow:0 10px 12px -4px rgba(229,9,20,.25)}.maintenance{max-width:520px;margin:13vh auto;padding:28px;border:1px solid #293040;border-radius:12px;background:#171c27;text-align:center}.maintenance details{text-align:left;margin-top:22px}.maintenance label{display:grid;gap:7px;margin:14px 0}.maintenance input{padding:9px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff;width:100%}@media(max-width:900px){.gallery.grid{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:560px){header{padding:10px 12px}.gallery{padding:10px}.gallery.grid{grid-template-columns:1fr}.gallery.grid .card.wide{grid-column:span 1}.lightbox-image{height:calc(94vh - 126px)}}
</style>
<style>
.gallery.slideshow{display:grid;min-height:calc(100vh - 80px);place-items:center}.gallery.slideshow .card{max-width:min(96vw,1280px)}.gallery.waterfall .card img{max-height:none}.lightbox-stage{position:relative;overflow:hidden;cursor:grab;touch-action:none;overscroll-behavior:contain}.lightbox-stage.is-dragging{cursor:grabbing}.lightbox-image{width:100%;height:100%;max-width:none;max-height:none;pointer-events:none;user-select:none;will-change:transform}.announcement{display:flex;max-height:min(78vh,680px);flex-direction:column}.announcement-main{display:flex;min-height:0;flex:1;flex-direction:column}.announcement-content{min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;padding-right:8px}.announcement-footer{flex:0 0 auto}body.announcement-open>#announcement{display:flex!important}
@media(max-width:560px){.gallery.waterfall{gap:8px;padding:8px}.waterfall-column{gap:8px}.announcement{width:92vw;height:min(68vh,520px);max-height:68vh;border-radius:14px}.announcement-main{padding:16px 18px 8px}.announcement-title{margin-bottom:10px;font-size:18px}.announcement-content{padding-right:5px;line-height:1.55}.announcement-content h1,.announcement-content h2,.announcement-content h3,.announcement-content h4{margin:10px 0 6px}.announcement-footer{padding:8px 18px 14px}.announcement-footer .meta{margin:4px 0 8px}.announcement-footer button{padding:8px 12px}dialog{width:100vw;height:100dvh;max-width:none;max-height:none;border:0;border-radius:0}.lightbox-stage{height:calc(100dvh - 126px)}.lightbox-image{height:100%}.lightbox-head,.lightbox-foot{padding:8px}.lightbox-controls{gap:5px}.lightbox-controls button{padding:8px 10px}}
</style>
<style>
.gallery.slideshow{min-height:calc(100vh - 176px)}.lightbox-stage{cursor:default}.lightbox-stage.can-pan{cursor:grab}.lightbox-stage.can-pan.is-dragging{cursor:grabbing}.lightbox-image{width:auto;height:auto;object-fit:fill}.slide-history{padding:8px 18px 14px;border-top:1px solid #293040;background:#10131a}.slide-history-track{display:flex;gap:8px;overflow-x:auto;padding:2px 1px 6px;scrollbar-width:thin;scroll-snap-type:x proximity}.slide-thumbnail{position:relative;flex:0 0 76px;width:76px;height:58px;padding:0;overflow:hidden;border:1px solid #3a455b;border-radius:6px;background:#080a0f;scroll-snap-align:center}.slide-thumbnail img{display:block;width:100%;height:100%;object-fit:cover}.slide-thumbnail.active{border-color:#fff;box-shadow:0 0 0 2px #4b8cff}.slide-thumbnail-index{position:absolute;right:3px;bottom:3px;min-width:19px;padding:1px 4px;border-radius:4px;background:#000b;color:#fff;font-size:11px}.preferences .check{display:flex;align-items:center;gap:8px}.preferences .check input{width:auto;margin:0}
@media(max-width:560px){.gallery.slideshow{min-height:calc(100dvh - 190px)}.slide-history{padding:7px 8px 10px}.slide-history-track{gap:6px}.slide-thumbnail{flex-basis:62px;width:62px;height:48px}}
</style>
<style>
.slide-history-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:7px}.slide-history-title{color:#a9b7cd;font-size:13px}.slide-history-latest{padding:6px 10px;background:#39445a;font-size:13px}
</style>
</head>
<body>
<header>
  <strong>图库</strong>
  <span class="meta" id="status">正在加载…</span>
  <span class="spacer"></span>
  <button id="previous" class="hidden" type="button">← 上一张</button>
  <button id="slideshow-toggle" class="hidden" type="button" aria-pressed="false">暂停</button>
  <button id="next" class="hidden" type="button">下一张 →</button>
  <button id="refresh" type="button">刷新</button>
  <button id="settings" type="button">显示设置</button>
  <button id="announcement-button" class="hidden" type="button">公告</button>
  <a href="/admin">管理</a>
</header>
<main id="gallery" class="gallery"></main>
<section id="slide-history" class="slide-history hidden" aria-label="播放历史"><div class="slide-history-head"><span class="slide-history-title">播放历史</span><button id="slide-history-latest" class="slide-history-latest" type="button">跳转到最新</button></div><div id="slide-history-track" class="slide-history-track"></div></section>
<section id="maintenance" class="maintenance hidden"><h1>维护中</h1><p>图片浏览暂时不可用，请稍后再试。</p><details><summary>管理员查看图片</summary><label>管理密钥<input id="maintenance-token" type="password" autocomplete="current-password"></label><button id="maintenance-unlock" type="button">查看图片</button><p id="maintenance-message" class="meta"></p></details></section>
<dialog id="lightbox">
  <div class="lightbox-head"><div class="lightbox-controls"><button id="rotate-left" type="button" title="向左旋转" aria-label="向左旋转">↶</button><button id="zoom-out" type="button" title="缩小" aria-label="缩小">−</button><button id="zoom-reset" type="button" title="复位">100%</button><button id="zoom-in" type="button" title="放大" aria-label="放大">＋</button><button id="rotate-right" type="button" title="向右旋转" aria-label="向右旋转">↷</button></div><button id="lightbox-close" type="button">关闭</button></div>
  <div id="lightbox-stage" class="lightbox-stage"><img id="lightbox-image" class="lightbox-image" alt="" draggable="false"></div>
  <div class="lightbox-foot"><div class="lightbox-meta"><p id="lightbox-caption" class="lightbox-caption"></p><p id="lightbox-directory" class="lightbox-directory"></p></div><button id="lightbox-download" type="button">下载</button></div>
</dialog>
<div id="preferences-backdrop" class="modal-backdrop hidden"></div>
<section id="preferences" class="preferences hidden" aria-label="显示设置">
  <h2>显示设置</h2>
  <label>视图<select id="layout-mode"><option value="slideshow">幻灯片</option><option value="waterfall">瀑布流</option></select></label>
  <label>幻灯片自动播放间隔（秒，0 表示关闭）<input id="slideshow-interval" type="number" min="0" max="300" step="1"></label>
  <label>图片间距（0–48 px，仅瀑布流有效）<input id="grid-gap" type="number" min="0" max="48"></label>
  <label>图片文字<select id="caption-mode"><option value="path">完整路径</option><option value="name">仅图片名称</option><option value="hidden">不展示</option></select></label>
  <label class="check"><input id="lightbox-directory-enabled" type="checkbox">在大图窗口中显示目录路径</label>
  <p class="meta">设置仅保存在当前浏览器。</p>
  <div class="preferences-actions"><button id="preferences-reset" type="button">恢复默认</button><button id="preferences-save" type="button">保存</button><button id="preferences-close" type="button">关闭</button></div>
</section>
<div id="announcement-backdrop" class="modal-backdrop announcement-backdrop hidden"></div>
<section id="announcement" class="announcement hidden" role="dialog" aria-modal="true" aria-labelledby="announcement-title">
  <div class="announcement-main"><h2 id="announcement-title" class="announcement-title"></h2><div id="announcement-content" class="announcement-content"></div></div>
  <div class="announcement-footer"><p id="announcement-reading" class="meta"></p><div class="announcement-actions"><button id="announcement-close-once" type="button">本次关闭</button><button id="announcement-close-forever" type="button">不再显示</button></div></div>
</section>
<script>
const PREFERENCE_KEY='openlist-image-preferences-v2';
const ANNOUNCEMENT_KEY_PREFIX='openlist-image-announcement-v2-';
const gallery=document.querySelector('#gallery');
const statusEl=document.querySelector('#status');
const previousButton=document.querySelector('#previous');
const nextButton=document.querySelector('#next');
const slideshowToggle=document.querySelector('#slideshow-toggle');
const refreshButton=document.querySelector('#refresh');
const settingsButton=document.querySelector('#settings');
const announcementButton=document.querySelector('#announcement-button');
const maintenance=document.querySelector('#maintenance');
const maintenanceToken=document.querySelector('#maintenance-token');
const maintenanceMessage=document.querySelector('#maintenance-message');
const preferencesPanel=document.querySelector('#preferences');
const preferencesBackdrop=document.querySelector('#preferences-backdrop');
const layoutMode=document.querySelector('#layout-mode');
const slideshowInterval=document.querySelector('#slideshow-interval');
const gridGap=document.querySelector('#grid-gap');
const captionMode=document.querySelector('#caption-mode');
const lightboxDirectoryEnabled=document.querySelector('#lightbox-directory-enabled');
const slideHistoryPanel=document.querySelector('#slide-history');
const slideHistoryTrack=document.querySelector('#slide-history-track');
const slideHistoryLatest=document.querySelector('#slide-history-latest');
const announcementPanel=document.querySelector('#announcement');
const announcementBackdrop=document.querySelector('#announcement-backdrop');
const announcementTitle=document.querySelector('#announcement-title');
const announcementContent=document.querySelector('#announcement-content');
const announcementReading=document.querySelector('#announcement-reading');
const announcementCloseOnce=document.querySelector('#announcement-close-once');
const announcementCloseForever=document.querySelector('#announcement-close-forever');
const lightbox=document.querySelector('#lightbox');
const lightboxStage=document.querySelector('#lightbox-stage');
const lightboxImage=document.querySelector('#lightbox-image');
const lightboxCaption=document.querySelector('#lightbox-caption');
const lightboxDirectory=document.querySelector('#lightbox-directory');
const lightboxDownload=document.querySelector('#lightbox-download');
const zoomOutButton=document.querySelector('#zoom-out');
const zoomResetButton=document.querySelector('#zoom-reset');
const zoomInButton=document.querySelector('#zoom-in');
const rotateLeftButton=document.querySelector('#rotate-left');
const rotateRightButton=document.querySelector('#rotate-right');
let settings=null;
let maintenanceAccessToken='';
let activeImage=null;
let announcementTimer=null;
let slideImages=[];
let slideIndex=0;
let slideLoadPromise=null;
let slideTimer=null;
let slideshowPaused=false;
let slideHistory=[];
let slideHistorySequence=0;
let waterfallLoading=false;
let loadedCount=0;
let cardSequence=0;
let waterfallColumnCount=0;
let waterfallAppendIndex=0;
let resizeTimer=null;
const slidePreloads=new Map();
const activePointers=new Map();
const SLIDE_PRELOAD_COUNT=3;
let dragStart=null;
let pinchStart=null;
let lightboxBaseWidth=0;
let lightboxBaseHeight=0;
let lastHiddenAt=0;
let recoveryPromise=null;
const URL_REFRESH_AGE_MS=10*60*1000;
const IDLE_RECOVERY_MS=5*60*1000;

function normalizedPreferences(value,defaults){
  const stored=value&&typeof value==='object'?value:{};
  const defaultLayout=defaults.view_layout==='waterfall'?'waterfall':'slideshow';
  const storedLayout=['single','grid'].includes(stored.view_layout)?'slideshow':stored.view_layout;
  const result={view_layout:storedLayout,slideshow_interval:stored.slideshow_interval,grid_gap:stored.grid_gap,caption_mode:stored.caption_mode,lightbox_directory_enabled:stored.lightbox_directory_enabled};
  if(!['slideshow','waterfall'].includes(result.view_layout)) result.view_layout=defaultLayout;
  if(!['path','name','hidden'].includes(result.caption_mode)) result.caption_mode=defaults.caption_mode;
  result.slideshow_interval=Math.max(0,Math.min(300,Number(result.slideshow_interval??8)||0));
  result.grid_gap=Math.max(0,Math.min(48,Number(result.grid_gap??defaults.grid_gap)||0));
  result.lightbox_directory_enabled=result.lightbox_directory_enabled===true;
  return result;
}

async function loadSettings(){
  const response=await fetch('/api/public-config',{cache:'no-store'});
  if(!response.ok) throw new Error('无法读取浏览设置');
  const defaults=await response.json();
  let stored={};
  try{stored=JSON.parse(localStorage.getItem(PREFERENCE_KEY)||'{}');}catch(error){localStorage.removeItem(PREFERENCE_KEY);}
  const preferences=normalizedPreferences(stored,defaults);
  preferences.default_view_layout=defaults.view_layout==='waterfall'?'waterfall':'slideshow';
  preferences.default_slideshow_interval=8;
  preferences.default_grid_gap=defaults.grid_gap;
  preferences.default_caption_mode=defaults.caption_mode;
  preferences.default_lightbox_directory_enabled=false;
  preferences.announcement=defaults.announcement;
  preferences.maintenance_enabled=defaults.maintenance_enabled;
  preferences.directory_display_enabled=defaults.directory_display_enabled;
  preferences.directory_display_depth=defaults.directory_display_depth;
  return preferences;
}

function imageName(path){const parts=path.split('/').filter(Boolean);return parts[parts.length-1]||path;}

function visiblePathFor(path){
  const parts=path.split('/').filter(Boolean);
  const name=imageName(path);
  if(!settings.directory_display_enabled) return name;
  const depth=Math.min(settings.directory_display_depth,Math.max(0,parts.length-1));
  return parts.slice(depth).join('/')||name;
}

function captionFor(image){
  if(settings.caption_mode==='hidden') return '';
  if(settings.caption_mode==='name') return imageName(image.path);
  return visiblePathFor(image.path);
}

function escapeHtml(value){return value.replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));}

function renderMarkdown(value){
  return escapeHtml(value).replace(/&lt;font\s+color=(?:&quot;|&#39;)?(#[0-9a-f]{3,8}|[a-z]+)(?:&quot;|&#39;)?\s*&gt;/gi,'<span style="color:$1">').replace(/&lt;\/font&gt;/gi,'</span>').replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>').replace(/^#### (.*)$/gm,'<h4>$1</h4>').replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>').replace(/\\n\\n/g,'</p><p>').replace(/\\n/g,'<br>');
}

function todayKey(){const now=new Date();return now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');}

function announcementSeen(key){
  try{const value=localStorage.getItem(key);return value==='forever'||value==='day:'+todayKey();}catch(error){return false;}
}

function closeAnnouncement(key,persist){
  if(announcementTimer) clearInterval(announcementTimer);
  localStorage.setItem(key,persist?'forever':'day:'+todayKey());
  announcementPanel.classList.add('is-closing');
  setTimeout(()=>{announcementPanel.classList.add('hidden');announcementPanel.classList.remove('is-closing');announcementBackdrop.classList.add('hidden');document.body.classList.remove('announcement-open');scheduleSlideshow();},220);
}

function setAnnouncementCountdown(seconds,key){
  if(announcementTimer) clearInterval(announcementTimer);
  let remaining=Math.max(0,Number(seconds)||0);
  const update=()=>{
    const locked=remaining>0;
    announcementCloseOnce.disabled=locked;
    announcementCloseForever.disabled=locked;
    announcementReading.textContent=locked?'请阅读 '+remaining+' 秒后再关闭':'';
    if(!locked&&announcementTimer){clearInterval(announcementTimer);announcementTimer=null;}
    remaining-=1;
  };
  update();
  if(remaining>=0&&seconds>0) announcementTimer=setInterval(update,1000);
  announcementCloseOnce.onclick=()=>closeAnnouncement(key,false);
  announcementCloseForever.onclick=()=>closeAnnouncement(key,true);
}

function showAnnouncement(force=false){
  const announcement=settings.announcement;
  if(!announcement||!announcement.enabled||!announcement.content.trim()) return;
  const key=ANNOUNCEMENT_KEY_PREFIX+announcement.version;
  if(!force&&announcementSeen(key)) return;
  clearSlideTimer();
  announcementTitle.textContent=announcement.title||'网站公告';
  announcementContent.innerHTML='<p>'+renderMarkdown(announcement.content)+'</p>';
  document.body.classList.add('announcement-open');
  announcementPanel.classList.remove('hidden');
  announcementPanel.classList.remove('is-closing');
  announcementBackdrop.classList.remove('hidden');
  setAnnouncementCountdown(force?0:announcement.required_seconds,key);
}

function persistPreferences(){localStorage.setItem(PREFERENCE_KEY,JSON.stringify({view_layout:settings.view_layout,slideshow_interval:settings.slideshow_interval,grid_gap:settings.grid_gap,caption_mode:settings.caption_mode,lightbox_directory_enabled:settings.lightbox_directory_enabled}));}

function openPreferences(){
  clearSlideTimer();
  layoutMode.value=settings.view_layout;
  slideshowInterval.value=settings.slideshow_interval;
  gridGap.value=settings.grid_gap;
  captionMode.value=settings.caption_mode;
  lightboxDirectoryEnabled.checked=settings.lightbox_directory_enabled;
  syncLightboxDirectoryControl();
  preferencesPanel.classList.remove('hidden');
  preferencesBackdrop.classList.remove('hidden');
}

function closePreferences(){preferencesPanel.classList.add('hidden');preferencesBackdrop.classList.add('hidden');scheduleSlideshow();}

function syncLightboxDirectoryControl(){
  const duplicatedByCaption=captionMode.value==='path';
  lightboxDirectoryEnabled.disabled=duplicatedByCaption;
  lightboxDirectoryEnabled.closest('label').title=duplicatedByCaption?'完整路径文字已包含目录，无需重复显示':'';
}

function directoryFor(image){
  if(!settings.directory_display_enabled) return '';
  const directories=image.path.split('/').filter(Boolean).slice(0,-1);
  if(!directories.length) return '根目录';
  const depth=Math.min(settings.directory_display_depth,directories.length);
  return directories.slice(depth).join('/')||'根目录';
}

function adminHeaders(){return maintenanceAccessToken?{'X-OpenList-Admin-Token':maintenanceAccessToken}:{};}

function wait(milliseconds){return new Promise(resolve=>setTimeout(resolve,milliseconds));}

async function fetchJsonWithRetry(url,options={},attempts=3){
  let lastError=null;
  for(let attempt=0;attempt<attempts;attempt+=1){
    const controller=new AbortController();
    const timeout=setTimeout(()=>controller.abort(),30000);
    try{
      const response=await fetch(url,{...options,signal:controller.signal,cache:'no-store'});
      if(!response.ok){
        const error=new Error('请求失败（HTTP '+response.status+'）');
        error.retryable=response.status>=500;
        throw error;
      }
      return await response.json();
    }catch(error){
      lastError=error;
      const retryable=error.name==='AbortError'||error.retryable!==false;
      if(!retryable||attempt===attempts-1) break;
      await wait(500*Math.pow(2,attempt));
    }finally{clearTimeout(timeout);}
  }
  throw lastError||new Error('请求失败');
}

async function refreshImageUrl(image,force=true){
  const fresh=force?'&fresh=1':'';
  const data=await fetchJsonWithRetry('/api/download-url?path='+encodeURIComponent(image.path)+fresh,{headers:adminHeaders()},2);
  image.url=data.url;
  image._resolvedAt=Date.now();
  return image;
}

async function ensureFreshImage(image){
  if(!image._resolvedAt||Date.now()-image._resolvedAt>URL_REFRESH_AGE_MS) await refreshImageUrl(image,true);
  return image;
}

function attachImageRecovery(element,image){
  element.addEventListener('load',()=>{delete element.dataset.refreshAttempted;},{once:false});
  element.addEventListener('error',()=>{
    if(element.dataset.refreshAttempted==='1') return;
    element.dataset.refreshAttempted='1';
    refreshImageUrl(image,true).then(()=>{element.src=image.url;}).catch(showError);
  });
}

async function downloadImage(){
  if(!activeImage) return;
  await refreshImageUrl(activeImage,true);
  const link=document.createElement('a');
  link.href=activeImage.url;
  link.click();
}

let zoomScale=1;
let zoomX=0;
let zoomY=0;
let zoomRotation=0;

function clampZoomPan(){
  const rect=lightboxStage.getBoundingClientRect();
  const quarterTurns=Math.abs(Math.round(zoomRotation/90))%2;
  const visualWidth=(quarterTurns?lightboxBaseHeight:lightboxBaseWidth)*zoomScale;
  const visualHeight=(quarterTurns?lightboxBaseWidth:lightboxBaseHeight)*zoomScale;
  const maxX=Math.max(0,(visualWidth-rect.width)/2);
  const maxY=Math.max(0,(visualHeight-rect.height)/2);
  zoomX=Math.max(-maxX,Math.min(maxX,zoomX));
  zoomY=Math.max(-maxY,Math.min(maxY,zoomY));
  lightboxStage.classList.toggle('can-pan',maxX>1||maxY>1);
}

function applyZoom(animate=true){
  clampZoomPan();
  lightboxImage.style.transition=animate?'transform .15s ease':'none';
  lightboxImage.style.transform='translate3d('+zoomX+'px,'+zoomY+'px,0) scale('+zoomScale+') rotate('+zoomRotation+'deg)';
  zoomResetButton.textContent=Math.round(zoomScale*100)+'%';
  zoomOutButton.disabled=zoomScale<=.5;
  zoomInButton.disabled=zoomScale>=4;
}

function resetZoom(){
  zoomScale=1;
  zoomX=0;
  zoomY=0;
  zoomRotation=0;
  applyZoom();
}

function fitLightboxImage(preserveZoom=false){
  if(!lightboxImage.naturalWidth||!lightboxImage.naturalHeight) return;
  const rect=lightboxStage.getBoundingClientRect();
  if(!rect.width||!rect.height) return;
  const fit=Math.min(rect.width/lightboxImage.naturalWidth,rect.height/lightboxImage.naturalHeight);
  lightboxBaseWidth=Math.max(1,Math.round(lightboxImage.naturalWidth*fit));
  lightboxBaseHeight=Math.max(1,Math.round(lightboxImage.naturalHeight*fit));
  lightboxImage.style.width=lightboxBaseWidth+'px';
  lightboxImage.style.height=lightboxBaseHeight+'px';
  if(preserveZoom) applyZoom(false);else resetZoom();
}

function changeZoom(nextScale,clientX=null,clientY=null){
  const next=Math.max(.5,Math.min(4,nextScale));
  if(clientX!==null&&clientY!==null&&zoomScale>0){
    const rect=lightboxStage.getBoundingClientRect();
    const pointX=clientX-rect.left-rect.width/2;
    const pointY=clientY-rect.top-rect.height/2;
    const ratio=next/zoomScale;
    zoomX=pointX-(pointX-zoomX)*ratio;
    zoomY=pointY-(pointY-zoomY)*ratio;
  }
  zoomScale=next;
  applyZoom();
}

function pointerDistance(points){return Math.hypot(points[0].x-points[1].x,points[0].y-points[1].y);}
function pointerCenter(points){return {x:(points[0].x+points[1].x)/2,y:(points[0].y+points[1].y)/2};}

function beginPointerGesture(){
  const points=[...activePointers.values()];
  if(points.length>=2){
    const pair=points.slice(0,2);
    pinchStart={distance:Math.max(1,pointerDistance(pair)),scale:zoomScale,x:zoomX,y:zoomY,center:pointerCenter(pair)};
    dragStart=null;
  }else if(points.length===1){
    dragStart={pointer:points[0].id,x:points[0].x,y:points[0].y,originX:zoomX,originY:zoomY};
    pinchStart=null;
  }
}

function openLightbox(image){
  clearSlideTimer();
  const caption=captionFor(image);
  const directory=settings.lightbox_directory_enabled&&settings.caption_mode!=='path'?directoryFor(image):'';
  activeImage=image;
  lightboxCaption.textContent=caption;
  lightboxCaption.classList.toggle('hidden',!caption);
  lightboxDirectory.textContent=directory?'目录：'+directory:'';
  lightboxDirectory.classList.toggle('hidden',!directory);
  lightboxBaseWidth=0;
  lightboxBaseHeight=0;
  lightbox.showModal();
  delete lightboxImage.dataset.refreshAttempted;
  lightboxImage.onload=()=>{delete lightboxImage.dataset.refreshAttempted;fitLightboxImage(false);};
  lightboxImage.onerror=()=>{
    if(lightboxImage.dataset.refreshAttempted==='1') return;
    lightboxImage.dataset.refreshAttempted='1';
    refreshImageUrl(image,true).then(()=>{lightboxImage.src=image.url;}).catch(showError);
  };
  lightboxImage.alt=imageName(image.path)||'OpenList 图片';
  lightboxImage.src=image.url;
  if(lightboxImage.complete&&lightboxImage.naturalWidth) fitLightboxImage(false);
}

function createCard(image,eager=false){
  const card=document.createElement('article');
  card.className='card';
  card.dataset.sequence=String(cardSequence++);
  const preview=document.createElement('button');
  preview.className='preview-button';
  preview.type='button';
  preview.setAttribute('aria-label','查看图片');
  preview.onclick=()=>openLightbox(image);
  const picture=document.createElement('img');
  picture.loading=eager?'eager':'lazy';
  picture.decoding='async';
  if(eager) picture.fetchPriority='high';
  picture.alt='OpenList 图片';
  picture.addEventListener('load',()=>card.classList.toggle('wide',picture.naturalWidth/picture.naturalHeight>=1.45),{once:true});
  attachImageRecovery(picture,image);
  picture.src=image.url;
  preview.append(picture);
  card.append(preview);
  return card;
}

async function requestImages(count){
  const data=await fetchJsonWithRetry('/api/images/random?count='+count+'&_='+Date.now(),{headers:adminHeaders()},3);
  const resolvedAt=Date.now();
  return data.images.map(image=>({...image,_resolvedAt:resolvedAt}));
}

function preferredWaterfallColumns(){
  const width=gallery.clientWidth||window.innerWidth;
  return width>900?3:2;
}

function appendWaterfallCard(card){
  const columns=[...gallery.children];
  columns[waterfallAppendIndex%columns.length].append(card);
  waterfallAppendIndex+=1;
}

function setupWaterfallColumns(){
  const count=preferredWaterfallColumns();
  const cards=[...gallery.querySelectorAll('.card')].sort((left,right)=>Number(left.dataset.sequence)-Number(right.dataset.sequence));
  waterfallColumnCount=count;
  waterfallAppendIndex=0;
  gallery.replaceChildren(...Array.from({length:count},()=>{const column=document.createElement('div');column.className='waterfall-column';return column;}));
  cards.forEach(appendWaterfallCard);
}

function applyGridStyle(){
  gallery.style.setProperty('--grid-gap',settings.grid_gap+'px');
}

function clearSlideTimer(){
  if(slideTimer){clearTimeout(slideTimer);slideTimer=null;}
}

function updateSlideshowToggle(){
  slideshowToggle.textContent=slideshowPaused?'继续':'暂停';
  slideshowToggle.setAttribute('aria-pressed',String(slideshowPaused));
}

function setSlideshowPaused(paused){
  slideshowPaused=paused;
  updateSlideshowToggle();
  if(paused) clearSlideTimer();else scheduleSlideshow();
  if(settings&&settings.view_layout==='slideshow'&&slideImages[slideIndex]) updateSlideshowStatus();
}

function scheduleSlideshow(delayMs=null){
  clearSlideTimer();
  if(!settings||settings.view_layout!=='slideshow'||slideshowPaused||document.hidden||lightbox.open||!preferencesPanel.classList.contains('hidden')||document.body.classList.contains('announcement-open')) return;
  if(delayMs===null&&settings.slideshow_interval<=0) return;
  slideTimer=setTimeout(()=>nextSlide().catch(showError),delayMs===null?settings.slideshow_interval*1000:delayMs);
}

function preloadUpcomingSlides(){
  const upcoming=slideImages.slice(slideIndex+1,slideIndex+1+SLIDE_PRELOAD_COUNT);
  const urls=new Set(upcoming.map(image=>image.url));
  for(const [url,image] of slidePreloads){if(!urls.has(url)){image.src='';slidePreloads.delete(url);}}
  for(const image of upcoming){
    if(slidePreloads.has(image.url)) continue;
    const preload=new Image();
    preload.decoding='async';
    preload.src=image.url;
    slidePreloads.set(image.url,preload);
  }
}

async function appendSlideImages(count){
  if(slideLoadPromise) return slideLoadPromise;
  slideLoadPromise=requestImages(count).then(images=>{slideImages.push(...images);return images;}).finally(()=>{slideLoadPromise=null;});
  return slideLoadPromise;
}

async function ensureSlideBuffer(){
  const remaining=slideImages.length-slideIndex-1;
  if(remaining<SLIDE_PRELOAD_COUNT) await appendSlideImages(SLIDE_PRELOAD_COUNT-remaining);
  const buffered=slideImages.slice(slideIndex+1,slideIndex+1+SLIDE_PRELOAD_COUNT);
  await Promise.allSettled(buffered.map(ensureFreshImage));
  preloadUpcomingSlides();
}

function recordSlideHistory(image){
  if(!image._historyId) image._historyId=++slideHistorySequence;
  if(slideHistory.some(item=>item._historyId===image._historyId)) return;
  slideHistory.push(image);
  if(slideHistory.length>60) slideHistory.splice(0,slideHistory.length-60);
}

async function showHistoryImage(image){
  setSlideshowPaused(true);
  await ensureFreshImage(image);
  let index=slideImages.findIndex(item=>item._historyId===image._historyId);
  if(index<0){
    slideImages.splice(slideIndex+1,0,image);
    index=slideIndex+1;
  }
  slideIndex=index;
  renderSlideshow();
  ensureSlideBuffer().then(()=>{updateSlideshowStatus();renderSlideHistory();}).catch(showError);
}

function renderSlideHistory(){
  const current=slideImages[slideIndex];
  slideHistoryTrack.replaceChildren(...slideHistory.map((image,index)=>{
    const button=document.createElement('button');
    button.className='slide-thumbnail'+(current&&current._historyId===image._historyId?' active':'');
    button.dataset.historyId=String(image._historyId);
    button.type='button';
    button.setAttribute('aria-label','切换到播放历史第 '+(index+1)+' 张');
    button.onclick=()=>showHistoryImage(image).catch(showError);
    const thumbnail=document.createElement('img');
    thumbnail.alt='';
    thumbnail.loading='lazy';
    attachImageRecovery(thumbnail,image);
    thumbnail.src=image.url;
    const badge=document.createElement('span');
    badge.className='slide-thumbnail-index';
    badge.textContent=String(index+1);
    button.append(thumbnail,badge);
    return button;
  }));
  const latest=slideHistory[slideHistory.length-1];
  slideHistoryLatest.disabled=!latest||Boolean(current&&current._historyId===latest._historyId);
  const active=slideHistoryTrack.querySelector('.active');
  if(active) active.scrollIntoView({behavior:'smooth',block:'nearest',inline:'center'});
}

function updateSlideshowStatus(){
  const autoText=slideshowPaused?' · 已暂停':settings.slideshow_interval>0?' · 自动 '+settings.slideshow_interval+' 秒':' · 自动播放已关闭';
  statusEl.textContent='幻灯片 · 第 '+(slideIndex+1)+' 张'+autoText;
}

function renderSlideshow(){
  const image=slideImages[slideIndex];
  gallery.className='gallery slideshow';
  gallery.replaceChildren();
  if(!image){
    const empty=document.createElement('p');
    empty.id='empty';
    empty.textContent='没有可用图片';
    gallery.append(empty);
    return;
  }
  gallery.append(createCard(image,true));
  recordSlideHistory(image);
  renderSlideHistory();
  previousButton.disabled=slideIndex===0;
  nextButton.disabled=false;
  updateSlideshowStatus();
  preloadUpcomingSlides();
  scheduleSlideshow();
}

async function loadSlideshow(reset){
  clearSlideTimer();
  if(reset){
    slideImages=[];
    slideIndex=0;
    slideHistory=[];
    slideHistorySequence=0;
    slidePreloads.clear();
    await appendSlideImages(SLIDE_PRELOAD_COUNT+1);
  }
  await ensureSlideBuffer();
  renderSlideshow();
}

async function nextSlide(){
  clearSlideTimer();
  await ensureSlideBuffer();
  if(slideIndex<slideImages.length-1) slideIndex+=1;
  await ensureFreshImage(slideImages[slideIndex]);
  if(slideIndex>80){slideImages=slideImages.slice(slideIndex-60);slideIndex=60;}
  renderSlideshow();
  ensureSlideBuffer().then(()=>{updateSlideshowStatus();renderSlideHistory();}).catch(showError);
}

function previousSlide(){
  clearSlideTimer();
  if(slideIndex>0) slideIndex-=1;
  renderSlideshow();
}

async function loadWaterfallBatch(reset){
  if(waterfallLoading) return;
  waterfallLoading=true;
  refreshButton.disabled=true;
  try{
    const images=await requestImages(15);
    if(reset){
      loadedCount=0;
      cardSequence=0;
      gallery.replaceChildren();
      setupWaterfallColumns();
    }
    const initial=loadedCount===0;
    images.forEach((image,index)=>appendWaterfallCard(createCard(image,initial&&index<2)));
    loadedCount+=images.length;
    statusEl.textContent='瀑布流 · 已加载 '+loadedCount+' 张';
  }finally{
    waterfallLoading=false;
    refreshButton.disabled=false;
  }
}

async function recoverAfterIdle(){
  if(recoveryPromise) return recoveryPromise;
  if(!settings||settings.maintenance_enabled&&!maintenanceAccessToken) return;
  recoveryPromise=(async()=>{
    if(settings.view_layout==='slideshow'){
      clearSlideTimer();
      statusEl.textContent='正在恢复图片连接…';
      const current=slideImages[slideIndex];
      if(current){
        try{await refreshImageUrl(current,true);}
        catch(error){
          const replacement=await requestImages(1);
          slideImages[slideIndex]=replacement[0];
        }
      }
      slidePreloads.clear();
      await ensureSlideBuffer();
      renderSlideshow();
    }else{
      await loadWaterfallBatch(false);
    }
  })().finally(()=>{recoveryPromise=null;});
  return recoveryPromise;
}

async function render(){
  clearSlideTimer();
  const restricted=settings.maintenance_enabled&&!maintenanceAccessToken;
  maintenance.classList.toggle('hidden',!restricted);
  gallery.classList.toggle('hidden',restricted);
  previousButton.classList.toggle('hidden',restricted||settings.view_layout!=='slideshow');
  nextButton.classList.toggle('hidden',restricted||settings.view_layout!=='slideshow');
  slideshowToggle.classList.toggle('hidden',restricted||settings.view_layout!=='slideshow');
  slideHistoryPanel.classList.toggle('hidden',restricted||settings.view_layout!=='slideshow');
  updateSlideshowToggle();
  refreshButton.disabled=restricted;
  if(restricted){statusEl.textContent='维护中';return;}
  applyGridStyle();
  if(settings.view_layout==='slideshow'){
    await loadSlideshow(true);
  }else{
    gallery.className='gallery waterfall';
    await loadWaterfallBatch(true);
  }
}

function showError(error){
  statusEl.textContent='加载失败：'+error.message;
  refreshButton.disabled=false;
  if(settings&&settings.view_layout==='slideshow'&&!slideshowPaused) scheduleSlideshow(15000);
}

previousButton.onclick=previousSlide;
nextButton.onclick=()=>nextSlide().catch(showError);
slideshowToggle.onclick=()=>setSlideshowPaused(!slideshowPaused);
slideHistoryLatest.onclick=()=>{const latest=slideHistory[slideHistory.length-1];if(latest)showHistoryImage(latest).catch(showError);};
refreshButton.onclick=()=>render().catch(showError);
settingsButton.onclick=openPreferences;
announcementButton.onclick=()=>showAnnouncement(true);
document.querySelector('#maintenance-unlock').onclick=async()=>{const token=maintenanceToken.value.trim();if(!token){maintenanceMessage.textContent='请输入管理密钥。';return;}maintenanceMessage.textContent='正在验证…';const response=await fetch('/api/admin/config',{headers:{'X-OpenList-Admin-Token':token},cache:'no-store'});if(!response.ok){maintenanceMessage.textContent='管理密钥无效。';return;}maintenanceAccessToken=token;maintenanceMessage.textContent='';render().catch(showError);};
lightboxDownload.onclick=()=>downloadImage().catch(showError);
document.querySelector('#preferences-save').onclick=()=>{settings.view_layout=layoutMode.value;settings.slideshow_interval=Math.max(0,Math.min(300,Number(slideshowInterval.value)||0));settings.grid_gap=Math.max(0,Math.min(48,Number(gridGap.value)||0));settings.caption_mode=captionMode.value;settings.lightbox_directory_enabled=lightboxDirectoryEnabled.checked;persistPreferences();closePreferences();render().catch(showError);};
document.querySelector('#preferences-reset').onclick=()=>{localStorage.removeItem(PREFERENCE_KEY);settings.view_layout=settings.default_view_layout;settings.slideshow_interval=settings.default_slideshow_interval;settings.grid_gap=settings.default_grid_gap;settings.caption_mode=settings.default_caption_mode;settings.lightbox_directory_enabled=settings.default_lightbox_directory_enabled;closePreferences();render().catch(showError);};
document.querySelector('#preferences-close').onclick=closePreferences;
preferencesBackdrop.onclick=closePreferences;
captionMode.onchange=syncLightboxDirectoryControl;
zoomOutButton.onclick=()=>changeZoom(zoomScale-.25);
zoomResetButton.onclick=resetZoom;
zoomInButton.onclick=()=>changeZoom(zoomScale+.25);
rotateLeftButton.onclick=()=>{zoomRotation-=90;applyZoom();};
rotateRightButton.onclick=()=>{zoomRotation+=90;applyZoom();};
document.querySelector('#lightbox-close').onclick=()=>lightbox.close();
lightbox.addEventListener('click',event=>{if(event.target===lightbox)lightbox.close();});
lightbox.addEventListener('close',()=>{activePointers.clear();dragStart=null;pinchStart=null;lightboxStage.classList.remove('is-dragging');scheduleSlideshow();});
lightboxStage.addEventListener('wheel',event=>{event.preventDefault();changeZoom(zoomScale*(event.deltaY<0?1.15:.87),event.clientX,event.clientY);},{passive:false});
lightboxStage.addEventListener('dblclick',event=>{event.preventDefault();if(zoomScale>1)resetZoom();else changeZoom(2,event.clientX,event.clientY);});
lightboxStage.addEventListener('pointerdown',event=>{
  event.preventDefault();
  lightboxStage.setPointerCapture(event.pointerId);
  activePointers.set(event.pointerId,{id:event.pointerId,x:event.clientX,y:event.clientY});
  lightboxStage.classList.add('is-dragging');
  beginPointerGesture();
});
lightboxStage.addEventListener('pointermove',event=>{
  if(!activePointers.has(event.pointerId)) return;
  event.preventDefault();
  activePointers.set(event.pointerId,{id:event.pointerId,x:event.clientX,y:event.clientY});
  const points=[...activePointers.values()];
  if(points.length>=2&&pinchStart){
    const pair=points.slice(0,2);
    const center=pointerCenter(pair);
    zoomScale=Math.max(.5,Math.min(4,pinchStart.scale*pointerDistance(pair)/pinchStart.distance));
    zoomX=pinchStart.x+(center.x-pinchStart.center.x);
    zoomY=pinchStart.y+(center.y-pinchStart.center.y);
    applyZoom(false);
  }else if(points.length===1&&dragStart&&lightboxStage.classList.contains('can-pan')){
    zoomX=dragStart.originX+points[0].x-dragStart.x;
    zoomY=dragStart.originY+points[0].y-dragStart.y;
    applyZoom(false);
  }
});
function finishPointer(event){
  activePointers.delete(event.pointerId);
  if(lightboxStage.hasPointerCapture(event.pointerId)) lightboxStage.releasePointerCapture(event.pointerId);
  if(activePointers.size) beginPointerGesture();
  else{dragStart=null;pinchStart=null;lightboxStage.classList.remove('is-dragging');applyZoom();}
}
lightboxStage.addEventListener('pointerup',finishPointer);
lightboxStage.addEventListener('pointercancel',finishPointer);
window.addEventListener('keydown',event=>{
  if(!lightbox.open) return;
  if(event.key==='Escape') lightbox.close();
  else if(event.key==='+'||event.key==='=') changeZoom(zoomScale+.25);
  else if(event.key==='-') changeZoom(zoomScale-.25);
  else if(event.key==='0') resetZoom();
  else if(event.key.toLowerCase()==='r'){zoomRotation+=90;applyZoom();}
});
window.addEventListener('scroll',()=>{
  if(settings&&settings.view_layout==='waterfall'&&window.scrollY+window.innerHeight>=document.documentElement.scrollHeight*.78){
    loadWaterfallBatch(false).catch(showError);
  }
},{passive:true});
window.addEventListener('resize',()=>{
  clearTimeout(resizeTimer);
  resizeTimer=setTimeout(()=>{
    if(!settings)return;
    applyGridStyle();
    if(settings.view_layout==='waterfall'&&waterfallColumnCount!==preferredWaterfallColumns())setupWaterfallColumns();
    if(lightbox.open) fitLightboxImage(true);
  },120);
},{passive:true});
document.addEventListener('visibilitychange',()=>{
  if(document.hidden){lastHiddenAt=Date.now();clearSlideTimer();return;}
  if(lastHiddenAt&&Date.now()-lastHiddenAt>=IDLE_RECOVERY_MS) recoverAfterIdle().catch(showError);
  else scheduleSlideshow();
  lastHiddenAt=0;
});
window.addEventListener('online',()=>recoverAfterIdle().catch(showError));
loadSettings().then(value=>{settings=value;announcementButton.classList.toggle('hidden',!settings.announcement.enabled);showAnnouncement();return render();}).catch(showError);
</script>
</body>
</html>"""


def admin_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>图库管理</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{max-width:1040px;margin:auto;padding:22px;background:#10131a;color:#e7edf7;font:15px system-ui,sans-serif}section{margin-bottom:18px;padding:18px;background:#171c27;border:1px solid #293040;border-radius:10px}h1,h2{margin-top:0}label{display:grid;gap:6px;margin:10px 0;color:#cbd6e7}.row{display:flex;gap:12px;flex-wrap:wrap}.row>label{flex:1;min-width:180px}input,select,textarea,button{font:inherit}input,select,textarea{width:100%;padding:9px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff}textarea{min-height:80px;resize:vertical}button{padding:9px 13px;border:0;border-radius:7px;background:#4b8cff;color:#fff;cursor:pointer}button.secondary{background:#39445a}.actions{margin-top:14px}.note{color:#a9b7cd}.directory{display:block;width:100%;margin:6px 0;text-align:left}.selected{display:grid;gap:5px;margin-top:10px}.selected-item{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:8px;border:1px solid #293040;border-radius:7px}.check{display:flex;align-items:center;gap:8px}.check input{width:auto}.markdown-preview{min-height:80px;padding:12px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff;line-height:1.65}.markdown-preview h1,.markdown-preview h2,.markdown-preview h3,.markdown-preview h4{margin:12px 0 7px}.markdown-preview p{margin:7px 0}.markdown-preview code{padding:2px 5px;border-radius:4px;background:#293040}.hidden{display:none}a{color:#b7d1ff}
</style>
</head>
<body>
<h1>图库管理</h1>
<p><a href="/gallery">返回图片浏览</a></p>
<section>
  <h2>服务器管理认证</h2>
  <label>WebUI 管理令牌<input id="token" type="password" autocomplete="current-password"></label>
  <button id="load" type="button">加载服务器配置</button>
  <p class="note hidden" id="admin-status" aria-live="polite"></p>
</section>
<section id="protected" class="hidden">
  <h2>全局服务器配置</h2>
  <p class="note">以下选项影响所有设备。并发浏览不会互相修改配置；若多名管理员同时保存，以最后一次保存为准。</p>
  <label>浏览 OpenList 目录<input id="path" value="/"></label>
  <div class="row actions"><button id="browse" type="button">列出子目录</button><button id="refresh-directory-cache" class="secondary" type="button">刷新目录缓存</button></div>
  <p class="note">单击目录进入下一层；双击目录添加到已选目录。刷新目录缓存会从 OpenList 重新读取目录树。</p>
  <div id="directories"></div>
  <h3>已选目录</h3>
  <div id="selected" class="selected"></div>
  <label>新访客的图片文字默认值<select id="default-caption"><option value="path">完整路径</option><option value="name">仅图片名称</option><option value="hidden">不展示</option></select></label>
  <h3>目录展示</h3>
  <label class="check"><input id="directory-display-enabled" type="checkbox">允许访客在“完整路径”模式下看到目录</label>
  <label>隐藏前 N 层目录（0 表示完整展示）<input id="directory-display-depth" type="number" min="0" max="64" step="1"></label>
  <p class="note">示例：路径 1/2/3/4，层级 1 显示为 2/3/4；层级 0 显示完整路径。</p>
  <h3>网站公告</h3>
  <label class="check"><input id="announcement-enabled" type="checkbox">启用公告弹窗</label>
  <label>公告标题<input id="announcement-title" maxlength="120"></label>
  <label>公告内容（Markdown）<textarea id="announcement-content" maxlength="4000" placeholder="# 标题&#10;支持 **加粗**、*斜体*、`代码`、链接和代码块"></textarea></label>
  <button id="announcement-preview-button" class="secondary" type="button">预览公告</button>
  <div id="announcement-preview" class="markdown-preview"></div>
  <label>强制阅读秒数（0–3600）<input id="announcement-required-seconds" type="number" min="0" max="3600" step="1"></label>
  <p class="note">启用后，访客必须等待指定秒数，才能关闭或设置当前公告版本不再显示。修改标题、内容、开关或秒数都会生成新公告版本。</p>
  <h3>维护模式</h3>
  <label class="check"><input id="maintenance-enabled" type="checkbox">启用维护模式</label>
  <p class="note">开启后主界面仅显示“维护中”；输入管理密钥并验证成功后，当前浏览会临时解锁图片查看和下载。</p>
  <div class="row actions"><button id="save-server" type="button">保存服务器配置</button><button id="rebuild" type="button">后台重建图片与目录索引</button><button id="backup" type="button">下载配置备份</button></div>
  <div class="row actions"><label>上传备份配置（ZIP）<input id="backup-file" type="file" accept=".zip,application/zip"></label><button id="restore-backup" class="secondary" type="button">上传并恢复备份</button></div>
  <p class="note">恢复仅覆盖可在本页面编辑的配置，不恢复管理密钥、OpenList 令牌、端口等系统设置。</p>
</section>
<script>
let config=null;
let rebuildTimer=null;
const adminStatus=document.querySelector('#admin-status');
function setAdminStatus(text){adminStatus.textContent=text;adminStatus.classList.toggle('hidden',!text);}
function auth(){return {'Content-Type':'application/json','X-OpenList-Admin-Token':document.querySelector('#token').value};}
function showSelected(){const root=document.querySelector('#selected');root.replaceChildren(...config.directories.map(path=>{const item=document.createElement('div');item.className='selected-item';const text=document.createElement('span');text.textContent=path;const remove=document.createElement('button');remove.className='secondary';remove.type='button';remove.textContent='移除';remove.onclick=()=>{config.directories=config.directories.filter(value=>value!==path);showSelected();};item.append(text,remove);return item;}));}
function escapeHtml(value){return value.replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));}
function renderMarkdown(value){return escapeHtml(value).replace(/&lt;font\s+color=(?:&quot;|&#39;)?(#[0-9a-f]{3,8}|[a-z]+)(?:&quot;|&#39;)?\s*&gt;/gi,'<span style="color:$1">').replace(/&lt;\/font&gt;/gi,'</span>').replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>').replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>').replace(/\\n\\n/g,'</p><p>').replace(/\\n/g,'<br>');}
function previewAnnouncement(){document.querySelector('#announcement-preview').innerHTML='<p>'+renderMarkdown(document.querySelector('#announcement-content').value)+'</p>';}
function showAdmin(){document.querySelector('#default-caption').value=config.caption_mode;document.querySelector('#directory-display-enabled').checked=config.directory_display_enabled;document.querySelector('#directory-display-depth').value=config.directory_display_depth;document.querySelector('#announcement-enabled').checked=config.announcement_enabled;document.querySelector('#announcement-title').value=config.announcement_title;document.querySelector('#announcement-content').value=config.announcement_content;document.querySelector('#announcement-required-seconds').value=config.announcement_required_seconds;document.querySelector('#maintenance-enabled').checked=config.maintenance_enabled;document.querySelector('#protected').classList.remove('hidden');showSelected();previewAnnouncement();}
async function errorText(response,fallback){try{const data=await response.json();return data.error||fallback;}catch(error){return fallback;}}
async function load(){const response=await fetch('/api/admin/config',{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'令牌无效或服务不可用'));config=await response.json();showAdmin();setAdminStatus('服务器配置已加载');}
let directoryClickTimer=null;
function addDirectory(path){if(!config.directories.includes(path)){config.directories.push(path);showSelected();setAdminStatus('已添加目录：'+path+'，请保存服务器配置');}}
async function browse(){if(!config)throw new Error('请先加载服务器配置');const path=document.querySelector('#path').value;const response=await fetch('/api/admin/directories?path='+encodeURIComponent(path),{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'无法读取目录缓存'));const data=await response.json();const root=document.querySelector('#directories');root.replaceChildren(...data.directories.map(item=>{const row=document.createElement('button');row.className='directory secondary';row.type='button';row.textContent=item.name+' ('+item.path+')';row.onclick=()=>{clearTimeout(directoryClickTimer);directoryClickTimer=setTimeout(()=>{document.querySelector('#path').value=item.path;browse().catch(report);},220);};row.ondblclick=event=>{event.preventDefault();clearTimeout(directoryClickTimer);addDirectory(item.path);};return row;}));if(!data.directories.length)root.textContent='当前目录没有缓存的子目录。请先刷新目录缓存，或重建图片与目录索引。';}
async function refreshDirectoryCache(){if(!config)throw new Error('请先加载服务器配置');const response=await fetch('/api/admin/directories/refresh',{method:'POST',headers:auth()});if(!response.ok)throw new Error(await errorText(response,'目录缓存刷新未启动'));setAdminStatus('目录缓存正在后台刷新');clearTimeout(rebuildTimer);rebuildTimer=setTimeout(()=>pollRebuild('目录缓存刷新完成').catch(report),2000);}
async function saveServer(){if(!config)throw new Error('请先加载服务器配置');const payload={directories:config.directories,caption_mode:document.querySelector('#default-caption').value,directory_display_enabled:document.querySelector('#directory-display-enabled').checked,directory_display_depth:Number(document.querySelector('#directory-display-depth').value),announcement_enabled:document.querySelector('#announcement-enabled').checked,announcement_title:document.querySelector('#announcement-title').value,announcement_content:document.querySelector('#announcement-content').value,announcement_required_seconds:Number(document.querySelector('#announcement-required-seconds').value),maintenance_enabled:document.querySelector('#maintenance-enabled').checked};const response=await fetch('/api/admin/config',{method:'PUT',headers:auth(),body:JSON.stringify(payload)});if(!response.ok)throw new Error(await errorText(response,'保存失败'));config=await response.json();showAdmin();setAdminStatus('全局服务器配置已保存；公告修改后将向访客显示新版本。');}
function formatSeconds(value){const seconds=Math.max(1,Math.round(Number(value)||0));return seconds<60?seconds+' 秒':Math.ceil(seconds/60)+' 分钟';}
async function pollRebuild(doneMessage='索引后台重建完成'){const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)return;if((await response.json()).refreshing){rebuildTimer=setTimeout(()=>pollRebuild(doneMessage).catch(report),2000);}else{setAdminStatus(doneMessage);rebuildTimer=null;}}
async function rebuild(){const statusResponse=await fetch('/api/status',{cache:'no-store'});const previous=statusResponse.ok?await statusResponse.json():{};const response=await fetch('/api/admin/rebuild',{method:'POST',headers:auth()});if(!response.ok)throw new Error(await errorText(response,'重建未启动'));const estimate=Number(previous.last_build_duration_seconds)||0;setAdminStatus('索引正在后台重建'+(estimate?'，预计约 '+formatSeconds(estimate):'，首次重建暂无预估时间'));clearTimeout(rebuildTimer);rebuildTimer=setTimeout(()=>pollRebuild().catch(report),2000);}
async function backup(){const response=await fetch('/api/admin/backup',{headers:auth()});if(!response.ok)throw new Error(await errorText(response,'备份下载失败'));const blob=await response.blob();const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='openlist-image-api-backup.zip';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);setAdminStatus('配置备份已下载（不含 token）');}
async function restoreBackup(){const file=document.querySelector('#backup-file').files[0];if(!file)throw new Error('请先选择 ZIP 备份文件');if(!window.confirm('确定恢复该备份中的可编辑配置吗？'))return;const response=await fetch('/api/admin/backup',{method:'POST',headers:{'X-OpenList-Admin-Token':document.querySelector('#token').value},body:file});if(!response.ok)throw new Error(await errorText(response,'备份恢复失败'));config=await response.json();showAdmin();setAdminStatus('备份配置已恢复，请按需保存或重建图片与目录索引。');}
function report(error){setAdminStatus('操作失败：'+error.message);}
document.querySelector('#load').onclick=()=>load().catch(report);
document.querySelector('#announcement-preview-button').onclick=previewAnnouncement;
document.querySelector('#browse').onclick=()=>browse().catch(report);
document.querySelector('#refresh-directory-cache').onclick=()=>refreshDirectoryCache().catch(report);
document.querySelector('#save-server').onclick=()=>saveServer().catch(report);
document.querySelector('#rebuild').onclick=()=>rebuild().catch(report);
document.querySelector('#backup').onclick=()=>backup().catch(report);
document.querySelector('#restore-backup').onclick=()=>restoreBackup().catch(report);
</script>
</body>
</html>"""


def make_handler(application: Application):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OpenListImageAPI/1.4"
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def _send_body(self, status: int, body: bytes, content_type: str, cache_control: str) -> None:
            compressed = "gzip" in self.headers.get("Accept-Encoding", "").lower() and len(body) >= 1024
            if compressed:
                body = gzip.compress(body, compresslevel=5)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.send_header("Vary", "Accept-Encoding")
            if compressed:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            self._send_body(status, json_bytes(payload), "application/json; charset=utf-8", "no-store")

        def _send_html(self, html: str) -> None:
            self._send_body(
                HTTPStatus.OK,
                html.encode("utf-8"),
                "text/html; charset=utf-8",
                "public, max-age=60, stale-while-revalidate=300",
            )

        def _send_attachment(self, filename: str, body: bytes) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", attachment_disposition(filename))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _proxy_download(self, image: dict[str, Any]) -> None:
            url = application.resolve_images([image])[0]["url"]
            request = Request(url, headers={"User-Agent": self.server_version})
            try:
                upstream = urlopen(request, timeout=60)
            except (HTTPError, URLError, TimeoutError) as error:
                raise RuntimeError("unable to download image from OpenList") from error
            with upstream:
                filename = Path(str(image["path"])).name or "image"
                content_length = upstream.headers.get("Content-Length")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", upstream.headers.get("Content-Type", "application/octet-stream"))
                self.send_header("Content-Disposition", attachment_disposition(filename))
                self.send_header("Cache-Control", "no-store")
                if content_length and content_length.isdigit():
                    self.send_header("Content-Length", content_length)
                else:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                try:
                    while chunk := upstream.read(64 * 1024):
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    logging.info("Download client disconnected: %s", self.client_address[0])

        def _admin_required(self) -> bool:
            try:
                allowed = application.is_admin(admin_token_from_headers(self.headers))
            except RuntimeError:
                allowed = False
            if not allowed:
                self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "admin authentication required"})
            return allowed

        def _maintenance_access_required(self) -> bool:
            if not application.config["maintenance_enabled"]:
                return False
            try:
                allowed = application.is_admin(admin_token_from_headers(self.headers))
            except RuntimeError:
                allowed = False
            if not allowed:
                self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "maintenance in progress", "maintenance": True})
            return not allowed

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
                if parsed.path == "/api/public-config":
                    return self._send_json(HTTPStatus.OK, application.visitor_config())
                if parsed.path == "/api/images/random":
                    if self._maintenance_access_required():
                        return
                    count = self._query_int(params, "count", 1)
                    images = application.choose_images(
                        count, params.get("folder", [None])[0], parse_size(params.get("min_size", [None])[0]), parse_size(params.get("max_size", [None])[0])
                    )
                    if not images:
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "no matching images"})
                    return self._send_json(HTTPStatus.OK, {"images": application.resolve_images(images)})
                if parsed.path == "/api/download-url":
                    if self._maintenance_access_required():
                        return
                    image = application.indexed_image(params.get("path", [""])[0])
                    refresh = params.get("fresh", ["0"])[0].lower() in {"1", "true", "yes"}
                    return self._send_json(HTTPStatus.OK, {"url": application.resolve_images([image], refresh=refresh)[0]["url"]})
                if parsed.path == "/download":
                    if self._maintenance_access_required():
                        return
                    image = application.indexed_image(params.get("path", [""])[0])
                    return self._proxy_download(image)
                if parsed.path == "/random":
                    if self._maintenance_access_required():
                        return
                    images = application.choose_images(1, params.get("folder", [None])[0], None, None)
                    if not images:
                        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "no matching images"})
                    url = application.resolve_images(images)[0]["url"]
                    self.send_response(HTTPStatus.FOUND)
                    self.send_header("Location", url)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    return
                if parsed.path == "/api/admin/config":
                    if self._admin_required():
                        return self._send_json(HTTPStatus.OK, application.admin_config())
                    return
                if parsed.path == "/api/admin/backup":
                    if self._admin_required():
                        return self._send_attachment("openlist-image-api-backup.zip", application.create_config_backup())
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
            path = urlparse(self.path).path
            if not self._admin_required():
                return
            try:
                if path == "/api/admin/rebuild":
                    if application.start_refresh():
                        return self._send_json(HTTPStatus.ACCEPTED, {"status": "rebuild started"})
                    return self._send_json(HTTPStatus.CONFLICT, {"error": "a rebuild is already running"})
                if path == "/api/admin/directories/refresh":
                    if application.start_directory_refresh():
                        return self._send_json(HTTPStatus.ACCEPTED, {"status": "directory refresh started"})
                    return self._send_json(HTTPStatus.CONFLICT, {"error": "an index refresh is already running"})
                if path == "/api/admin/backup":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_REQUEST_BODY:
                        raise ValueError("invalid request body size")
                    return self._send_json(HTTPStatus.OK, application.restore_config_backup(self.rfile.read(length)))
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (ValueError, RuntimeError, zipfile.BadZipFile) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception:
                logging.exception("Unhandled POST error")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

        def log_message(self, message: str, *args: object) -> None:
            logging.info("%s %s", self.client_address[0], message % args)

    return Handler


class ConcurrentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128
    allow_reuse_address = True


def command_serve(config_path: Path) -> None:
    application = Application(config_path)
    server = ConcurrentHTTPServer((application.config["listen_host"], application.config["listen_port"]), make_handler(application))
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
