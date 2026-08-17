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
import subprocess
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
    "theme": "dark",
    "grid_gap": 12,
    "grid_scale": 150,
    "url_cache_size": 0,
    "url_cache_ttl_seconds": 1800,
    "tagging_enabled": False,
    "tagging_scope": "anonymous",
    "tagging_categories": [],
    "tagging_allow_custom": False,
    "tagging_sort_default": "likes",
    "filter_enabled": True,
    "log_level": "INFO",
    "admin_token_file": "/etc/openlist-image-api/admin.token",
}
ALLOWED_LAYOUTS = {"single", "grid", "waterfall"}
ALLOWED_DELIVERY = {"preview", "download"}
ALLOWED_CAPTION_MODES = {"path", "name", "hidden"}
MAX_REQUEST_BODY = 64 * 1024
URL_RESOLVE_WORKERS = 20
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
    if config["theme"] not in {"light", "dark"}:
        raise ValueError("theme must be light or dark")
    if not isinstance(config["grid_gap"], int) or not 0 <= config["grid_gap"] <= 48:
        raise ValueError("grid_gap must be between 0 and 48")
    if not isinstance(config["grid_scale"], int) or not 75 <= config["grid_scale"] <= 200:
        raise ValueError("grid_scale must be between 75 and 200")
    if not isinstance(config["url_cache_size"], int) or not 0 <= config["url_cache_size"] <= 5000:
        raise ValueError("invalid url_cache_size")
    if not isinstance(config["url_cache_ttl_seconds"], int) or not 0 <= config["url_cache_ttl_seconds"] <= 3600:
        raise ValueError("invalid url_cache_ttl_seconds")
    if not isinstance(config["tagging_enabled"], bool):
        raise ValueError("tagging_enabled must be a boolean")
    if config["tagging_scope"] not in {"disabled", "anonymous", "token"}:
        raise ValueError("tagging_scope must be disabled, anonymous or token")
    if not isinstance(config["tagging_categories"], list):
        raise ValueError("tagging_categories must be a list")
    config["tagging_categories"] = [str(c).strip() for c in config["tagging_categories"] if str(c).strip()]
    if len(config["tagging_categories"]) > 32:
        raise ValueError("tagging_categories is too long (max 32)")
    if not isinstance(config["tagging_allow_custom"], bool):
        raise ValueError("tagging_allow_custom must be a boolean")
    if config["tagging_sort_default"] not in {"likes", "dislikes", "ratio"}:
        raise ValueError("tagging_sort_default must be likes, dislikes or ratio")
    if not isinstance(config["filter_enabled"], bool):
        raise ValueError("filter_enabled must be a boolean")
    if config["log_level"] not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ValueError("log_level must be DEBUG, INFO, WARNING or ERROR")
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


_secret_cache: dict[str, tuple[float, str]] = {}
_secret_cache_lock = threading.Lock()


def read_secret_cached(path: Path, name: str) -> str:
    cache_key = str(path)
    with _secret_cache_lock:
        cached = _secret_cache.get(cache_key)
        if cached is not None:
            try:
                mtime = path.stat().st_mtime
                if mtime == cached[0]:
                    return cached[1]
            except OSError:
                pass
    value = read_secret(path, name)
    with _secret_cache_lock:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = time.time()
        _secret_cache[cache_key] = (mtime, value)
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
        token = read_secret_cached(self.token_path, "OpenList API token")
        post_start = time.time()
        request = Request(
            f"{self.base_url}{endpoint}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": token, "Content-Type": "application/json"},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                with urlopen(request, timeout=15) as response:
                    result = json.load(response)
                if result.get("code") != 200:
                    raise RuntimeError(result.get("message") or "OpenList rejected request")
                data = result.get("data") or {}
                if not isinstance(data, dict):
                    raise RuntimeError("OpenList returned invalid data")
                logging.debug("OpenList %s: %.3fs (attempt %d)", endpoint, time.time() - post_start, attempt + 1)
                return data
            except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as error:
                last_error = error
                logging.debug("OpenList %s failed attempt %d: %s", endpoint, attempt + 1, error)
                if attempt < 1:
                    time.sleep(0.5)
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

    def resolve_file(self, path: str) -> tuple[str, str]:
        data = self._post("/api/fs/get", {"path": path, "password": "", "refresh": False})
        url = str(data.get("raw_url") or data.get("url") or "").strip()
        if not url:
            raise RuntimeError("OpenList did not return a file URL")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("OpenList returned an invalid file URL")
        thumb = str(data.get("thumb") or "").strip()
        if thumb:
            parsed_thumb = urlparse(thumb)
            if parsed_thumb.scheme not in {"http", "https"} or not parsed_thumb.netloc:
                thumb = ""
        return url, thumb

    def remove_file(self, path: str) -> None:
        parts = path.rsplit("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise RuntimeError(f"invalid file path for removal: {path}")
        dir_path, name = parts[0], parts[1]
        self._post("/api/fs/remove", {"dir": dir_path, "names": [name]})


class IndexRepository:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "index.json"
        self._lock = threading.Lock()
        self._cache: dict[str, Any] | None = None
        self._cache_mtime: float = 0

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                self._cache = None
                return {"images": [], "directories": [], "generated_at": 0, "errors": []}
            try:
                mtime = self.path.stat().st_mtime
                if self._cache is not None and mtime == self._cache_mtime:
                    return self._cache
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"unable to read image index: {error}") from error
            if not isinstance(data, dict) or not isinstance(data.get("images"), list):
                raise RuntimeError("image index is invalid")
            self._cache = data
            self._cache_mtime = mtime
            return data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            atomic_write_json(self.path, data)
            self._cache = data
            self._cache_mtime = self.path.stat().st_mtime if self.path.exists() else 0


class TagRepository:
    def __init__(self, state_dir: Path):
        self.path = state_dir / "tags.json"
        self._lock = threading.RLock()
        self._cache: dict[str, Any] | None = None

    def _default(self) -> dict[str, Any]:
        return {"schema_version": 1, "tags": {}, "updated_at": 0}

    def load(self) -> dict[str, Any]:
        with self._lock:
            if self._cache is not None:
                return self._cache
            if not self.path.exists():
                self._cache = self._default()
                return self._cache
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"unable to read tags store: {error}") from error
            if not isinstance(data, dict) or not isinstance(data.get("tags"), dict):
                data = self._default()
            data.setdefault("schema_version", 1)
            data.setdefault("tags", {})
            data.setdefault("updated_at", 0)
            self._cache = data
            return self._cache

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            data["updated_at"] = int(time.time())
            atomic_write_json(self.path, data)
            self._cache = data

    def stats(self, paths: list[str]) -> dict[str, Any]:
        tags = self.load().get("tags", {})
        result = {}
        for path in paths:
            entry = tags.get(path)
            if entry:
                result[path] = {
                    "likes": int(entry.get("likes", 0)),
                    "dislikes": int(entry.get("dislikes", 0)),
                    "categories": list(entry.get("categories", [])),
                }
            else:
                result[path] = {"likes": 0, "dislikes": 0, "categories": []}
        return result

    def vote(self, path: str, voter_id: str, vote_type: str, value: bool) -> dict[str, Any]:
        with self._lock:
            data = self.load()
            tags = data.setdefault("tags", {})
            entry = tags.setdefault(path, {"likes": 0, "dislikes": 0, "categories": [], "voters": {}})
            voters = entry.setdefault("voters", {})
            previous = voters.get(voter_id)
            if previous == vote_type and value:
                return self._summary(entry)
            if previous and previous != vote_type and value:
                if previous == "like":
                    entry["likes"] = max(0, int(entry.get("likes", 0)) - 1)
                elif previous == "dislike":
                    entry["dislikes"] = max(0, int(entry.get("dislikes", 0)) - 1)
            if value:
                voters[voter_id] = vote_type
                if vote_type == "like":
                    entry["likes"] = int(entry.get("likes", 0)) + 1
                elif vote_type == "dislike":
                    entry["dislikes"] = int(entry.get("dislikes", 0)) + 1
            else:
                if previous == vote_type:
                    voters.pop(voter_id, None)
                    if vote_type == "like":
                        entry["likes"] = max(0, int(entry.get("likes", 0)) - 1)
                    elif vote_type == "dislike":
                        entry["dislikes"] = max(0, int(entry.get("dislikes", 0)) - 1)
            self.save(data)
            return self._summary(entry)

    def set_category(self, path: str, category: str, value: bool) -> dict[str, Any]:
        with self._lock:
            data = self.load()
            tags = data.setdefault("tags", {})
            entry = tags.setdefault(path, {"likes": 0, "dislikes": 0, "categories": [], "voters": {}})
            categories = entry.setdefault("categories", [])
            if value and category not in categories:
                categories.append(category)
            elif not value and category in categories:
                categories.remove(category)
            self.save(data)
            return self._summary(entry)

    def paths_for_tag(self, tag: str) -> set[str]:
        tags = self.load().get("tags", {})
        result = set()
        for path, entry in tags.items():
            if tag in entry.get("categories", []):
                result.add(path)
        return result

    def all_categories(self) -> dict[str, int]:
        tags = self.load().get("tags", {})
        counts: dict[str, int] = {}
        for entry in tags.values():
            for category in entry.get("categories", []):
                counts[category] = counts.get(category, 0) + 1
        return counts

    def reset_path(self, path: str) -> None:
        with self._lock:
            data = self.load()
            data.get("tags", {}).pop(path, None)
            self.save(data)

    def reset_all(self) -> None:
        with self._lock:
            self._cache = self._default()
            atomic_write_json(self.path, self._cache)

    def migrate_paths(self, valid_paths: set[str]) -> dict[str, int]:
        with self._lock:
            data = self.load()
            tags = data.get("tags", {})
            old_tags = dict(tags)
            tags.clear()
            migrated = 0
            orphaned = 0
            path_index = {p.lower(): p for p in valid_paths}
            for old_path, entry in old_tags.items():
                if old_path in valid_paths:
                    tags[old_path] = entry
                    migrated += 1
                    continue
                lower_old = old_path.lower()
                if lower_old in path_index and path_index[lower_old] != old_path:
                    tags[path_index[lower_old]] = entry
                    migrated += 1
                    continue
                old_suffix = "/".join(old_path.split("/")[-3:]).lower()
                matched = None
                for new_path in valid_paths:
                    new_suffix = "/".join(new_path.split("/")[-3:]).lower()
                    if new_suffix == old_suffix:
                        matched = new_path
                        break
                if matched:
                    if matched not in tags:
                        tags[matched] = entry
                        migrated += 1
                else:
                    orphaned += 1
            self.save(data)
            return {"migrated": migrated, "orphaned": orphaned, "total": len(old_tags)}

    @staticmethod
    def _summary(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            "likes": int(entry.get("likes", 0)),
            "dislikes": int(entry.get("dislikes", 0)),
            "categories": list(entry.get("categories", [])),
        }


def join_virtual_path(parent: str, child: str) -> str:
    return normalize_directory(f"{parent.rstrip('/')}/{child}")


def build_index(config: dict[str, Any], repository: IndexRepository) -> dict[str, Any]:
    started_at = time.time()
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
        "version": 2,
        "generated_at": int(time.time()),
        "build_duration_seconds": round(time.time() - started_at, 2),
        "directories": config["directories"],
        "directory_count": len(visited),
        "image_count": len(images),
        "errors": errors,
        "images": images,
    }
    repository.save(index)
    return index


class _InflightResolve:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: tuple[str, str] | None = None
        self.error: BaseException | None = None


class UrlCache:
    def __init__(self, max_size: int, ttl_seconds: int):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[float, str, str]] = OrderedDict()
        self._lock = threading.Lock()
        self._inflight: dict[str, _InflightResolve] = {}
        self.hits = 0
        self.misses = 0

    def _cached_unlocked(self, path: str, count_hit: bool = True) -> tuple[str, str] | None:
        cached = self._entries.get(path)
        if not cached:
            return None
        if time.monotonic() - cached[0] < self.ttl_seconds:
            if count_hit:
                self.hits += 1
            self._entries.move_to_end(path)
            return cached[1], cached[2]
        self._entries.pop(path, None)
        return None

    def _cached(self, path: str) -> tuple[str, str] | None:
        with self._lock:
            return self._cached_unlocked(path)

    def cached_url(self, path: str) -> str | None:
        cached = self._cached(path)
        return cached[0] if cached else None

    def cached_thumb(self, path: str) -> str | None:
        cached = self._cached(path)
        return cached[1] if cached else None

    def resolve(self, path: str, client: OpenListClient, refresh: bool = False) -> tuple[str, str]:
        if not refresh:
            cached = self._cached(path)
            if cached is not None:
                return cached
        with self._lock:
            inflight = self._inflight.get(path)
            if inflight is None:
                if not refresh:
                    cached = self._cached_unlocked(path)
                    if cached is not None:
                        return cached
                inflight = _InflightResolve()
                self._inflight[path] = inflight
                self.misses += 1
                if refresh:
                    self._entries.pop(path, None)
                leader = True
            else:
                leader = False
        if not leader:
            inflight.event.wait()
            if inflight.error is not None:
                raise inflight.error
            if inflight.result is None:
                raise RuntimeError("url resolve produced no result")
            return inflight.result
        try:
            url, thumb = client.resolve_file(path)
            if self.max_size:
                with self._lock:
                    self._entries[path] = (time.monotonic(), url, thumb)
                    self._entries.move_to_end(path)
                    while len(self._entries) > self.max_size:
                        self._entries.popitem(last=False)
            inflight.result = (url, thumb)
            return url, thumb
        except Exception as error:
            inflight.error = error
            raise
        finally:
            with self._lock:
                if self._inflight.get(path) is inflight:
                    del self._inflight[path]
            inflight.event.set()

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"size": len(self._entries), "hits": self.hits, "misses": self.misses}


class Application:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.repository = IndexRepository(Path(self.config["state_dir"]))
        self.tags = TagRepository(Path(self.config["state_dir"]))
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
        self.tags = TagRepository(Path(self.config["state_dir"]))
        cache_changed = any(
            previous_config[key] != self.config[key]
            for key in ("url_cache_size", "url_cache_ttl_seconds", "openlist_api_url", "openlist_token_file")
        )
        if cache_changed:
            self.cache = UrlCache(self.config["url_cache_size"], self.config["url_cache_ttl_seconds"])
        self.apply_log_level()
        return self.config

    def apply_log_level(self) -> None:
        level_name = self.config.get("log_level", "INFO")
        level = getattr(logging, level_name, logging.INFO)
        root_logger = logging.getLogger()
        if root_logger.level != level:
            root_logger.setLevel(level)
            logging.info("Log level set to %s", level_name)

    def visitor_config(self) -> dict[str, Any]:
        config = DEVICE_PREFERENCE_DEFAULTS.copy()
        config["caption_mode"] = self.config["caption_mode"]
        config["directory_display_enabled"] = self.config["directory_display_enabled"]
        config["directory_display_depth"] = self.config["directory_display_depth"]
        config["theme"] = self.config["theme"]
        config["announcement"] = {
            "enabled": self.config["announcement_enabled"],
            "title": self.config["announcement_title"] if self.config["announcement_enabled"] else "",
            "content": self.config["announcement_content"] if self.config["announcement_enabled"] else "",
            "required_seconds": self.config["announcement_required_seconds"] if self.config["announcement_enabled"] else 0,
            "version": self.config["announcement_version"],
        }
        config["maintenance_enabled"] = self.config["maintenance_enabled"]
        config["filter_enabled"] = self.config["filter_enabled"]
        config["tagging"] = {
            "enabled": self.config["tagging_enabled"] and self.config["tagging_scope"] != "disabled",
            "scope": self.config["tagging_scope"],
            "categories": self.config["tagging_categories"],
            "allow_custom": self.config["tagging_allow_custom"],
            "sort_default": self.config["tagging_sort_default"],
            "trash_tag": self.TRASH_TAG,
        }
        return config

    def public_config(self) -> dict[str, Any]:
        return self.visitor_config()

    def admin_config(self) -> dict[str, Any]:
        return {
            "directories": self.config["directories"],
            "caption_mode": self.config["caption_mode"],
            "directory_display_enabled": self.config["directory_display_enabled"],
            "directory_display_depth": self.config["directory_display_depth"],
            "theme": self.config["theme"],
            "announcement_enabled": self.config["announcement_enabled"],
            "announcement_title": self.config["announcement_title"],
            "announcement_content": self.config["announcement_content"],
            "announcement_required_seconds": self.config["announcement_required_seconds"],
            "announcement_version": self.config["announcement_version"],
            "maintenance_enabled": self.config["maintenance_enabled"],
            "tagging_enabled": self.config["tagging_enabled"],
            "tagging_scope": self.config["tagging_scope"],
            "tagging_categories": self.config["tagging_categories"],
            "tagging_allow_custom": self.config["tagging_allow_custom"],
            "tagging_sort_default": self.config["tagging_sort_default"],
            "filter_enabled": self.config["filter_enabled"],
            "log_level": self.config["log_level"],
        }

    def update_admin_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "directories",
            "caption_mode",
            "directory_display_enabled",
            "directory_display_depth",
            "theme",
            "announcement_enabled",
            "announcement_title",
            "announcement_content",
            "announcement_required_seconds",
            "maintenance_enabled",
            "tagging_enabled",
            "tagging_scope",
            "tagging_categories",
            "tagging_allow_custom",
            "tagging_sort_default",
            "filter_enabled",
            "log_level",
        }
        if set(payload.keys()) - allowed:
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
                "theme": self.config["theme"],
                "announcement_enabled": self.config["announcement_enabled"],
                "announcement_title": self.config["announcement_title"],
                "announcement_content": self.config["announcement_content"],
                "announcement_required_seconds": self.config["announcement_required_seconds"],
                "announcement_version": self.config["announcement_version"],
                "maintenance_enabled": self.config["maintenance_enabled"],
                "tagging_enabled": self.config["tagging_enabled"],
                "tagging_scope": self.config["tagging_scope"],
                "tagging_categories": self.config["tagging_categories"],
                "tagging_allow_custom": self.config["tagging_allow_custom"],
                "tagging_sort_default": self.config["tagging_sort_default"],
                "filter_enabled": self.config["filter_enabled"],
                "log_level": self.config["log_level"],
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
            "theme",
            "announcement_enabled",
            "announcement_title",
            "announcement_content",
            "announcement_required_seconds",
            "maintenance_enabled",
            "tagging_enabled",
            "tagging_scope",
            "tagging_categories",
            "tagging_allow_custom",
            "tagging_sort_default",
            "filter_enabled",
            "log_level",
        }
        payload = {key: value for key, value in backup["config"].items() if key in allowed}
        if not payload:
            raise ValueError("backup has no restorable configuration")
        return self.update_admin_config(payload)

    def is_admin(self, supplied_token: str | None) -> bool:
        if not supplied_token:
            return False
        expected = read_secret_cached(Path(self.config["admin_token_file"]), "admin token")
        return hmac.compare_digest(supplied_token, expected)

    def start_refresh(self) -> bool:
        if not self.refresh_lock.acquire(blocking=False):
            return False
        self.refreshing = True

        def worker() -> None:
            try:
                index = build_index(self.config, self.repository)
                valid_paths = {img["path"] for img in index.get("images", []) if isinstance(img, dict) and "path" in img}
                if valid_paths:
                    stats = self.tags.migrate_paths(valid_paths)
                    if stats.get("migrated", 0) or stats.get("orphaned", 0):
                        logging.info("Tag path migration: %s", stats)
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
            "last_build_duration_seconds": float(index.get("build_duration_seconds") or 0),
            "refreshing": self.refreshing,
            "last_refresh_error": self.last_refresh_error,
            "cache": self.cache.status(),
            **self.public_config(),
        }

    def list_directories(self, path: str) -> list[dict[str, Any]]:
        """List subdirectories of a virtual path live from OpenList."""
        directory = normalize_directory(path)
        try:
            entries = OpenListClient(self.config).list_directory(directory)
        except Exception as error:
            if directory != "/":
                raise RuntimeError(f"unable to list directory {directory}: {error}") from error
            roots = [{"name": item.rsplit("/", 1)[-1] or "/", "path": item} for item in self.config["directories"]]
            return sorted(roots, key=lambda item: item["name"].casefold())
        children: list[dict[str, Any]] = []
        seen: set[str] = set()
        for entry in entries:
            name = str(entry.get("name") or "")
            if not entry.get("is_dir") or not name or "/" in name or "\\" in name:
                continue
            child_path = join_virtual_path(directory, name)
            if child_path in seen:
                continue
            seen.add(child_path)
            children.append({"name": name, "path": child_path})
        children.sort(key=lambda item: item["name"].casefold())
        return children

    def choose_images(self, count: int, folder: str | None, min_size: int | None, max_size: int | None, tags: list[str] | None = None, filter_mode: str = "union") -> list[dict[str, Any]]:
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
        if tags:
            if filter_mode == "intersect":
                path_sets = [self.tags.paths_for_tag(tag) for tag in tags]
                if path_sets:
                    allowed_paths: set[str] = set.intersection(*path_sets)
                else:
                    allowed_paths = set()
            else:
                allowed_paths = set()
                for tag in tags:
                    allowed_paths |= self.tags.paths_for_tag(tag)
            images = [image for image in images if image.get("path", "") in allowed_paths]
        if not images:
            return []
        count = max(1, min(count, 50))
        if count <= len(images):
            return random.sample(images, count)
        return [random.choice(images) for _ in range(count)]

    def indexed_image(self, path: str) -> dict[str, Any]:
        matches = self.indexed_images([path])
        if not matches or matches[0].get("_missing"):
            raise ValueError("image is not in the current index")
        return {key: value for key, value in matches[0].items() if key != "_missing"}

    def indexed_images(self, paths: list[str]) -> list[dict[str, Any]]:
        wanted: list[str] = []
        seen: set[str] = set()
        for raw_path in paths:
            try:
                path = normalize_directory(str(raw_path))
            except ValueError:
                continue
            if not path or path in seen:
                continue
            seen.add(path)
            wanted.append(path)
        if not wanted:
            return []
        lookup: dict[str, dict[str, Any]] = {}
        remaining = set(wanted)
        for image in self.repository.load().get("images", []):
            path = str(image.get("path", ""))
            if path in remaining:
                lookup[path] = image
                remaining.remove(path)
                if not remaining:
                    break
        resolved = []
        for path in wanted:
            if path in lookup:
                resolved.append(lookup[path])
            else:
                resolved.append({"path": path, "size": 0, "_missing": True})
        return resolved

    def resolve_images(self, images: list[dict[str, Any]], refresh: bool = False, include_tags: bool = False) -> list[dict[str, Any]]:
        if not images:
            return []
        client = OpenListClient(self.config)
        paths = [str(image["path"]) for image in images]
        tag_stats = self.tags.stats(paths) if include_tags else {}
        started_at = time.time()

        def resolve(image: dict[str, Any]) -> dict[str, Any]:
            path = str(image["path"])
            url, thumb = self.cache.resolve(path, client, refresh=True) if refresh else self.cache.resolve(path, client)
            result = {"path": path, "size": int(image.get("size") or 0), "url": url, "thumbnail": thumb}
            if include_tags:
                result["tags"] = tag_stats.get(path, {"likes": 0, "dislikes": 0, "categories": []})
            return result

        results = [resolve(images[0])] if len(images) == 1 else list(self.url_executor.map(resolve, images))
        cache_hits = getattr(self.cache, "hits", 0)
        cache_misses = getattr(self.cache, "misses", 0)
        logging.debug("resolve_images: %d images in %.3fs (cache hits=%d misses=%d)", len(images), time.time() - started_at, cache_hits, cache_misses)
        return results

    def resolve_download_urls(self, paths: list[str], refresh: bool = False) -> list[dict[str, Any]]:
        images = self.indexed_images(paths[:50])
        if not images:
            return []
        client = OpenListClient(self.config)

        def resolve_one(image: dict[str, Any]) -> dict[str, Any]:
            path = str(image["path"])
            if image.get("_missing"):
                return {"path": path, "error": "image is not in the current index"}
            try:
                url, thumb = self.cache.resolve(path, client, refresh=refresh)
            except Exception:
                logging.warning("Failed to resolve download URL for %s", path)
                return {"path": path, "error": "unable to resolve image URL"}
            return {"path": path, "url": url, "thumbnail": thumb or ""}

        if len(images) == 1:
            return [resolve_one(images[0])]
        return list(self.url_executor.map(resolve_one, images))

    def resolve_images_lazy(self, images: list[dict[str, Any]], include_tags: bool = False) -> list[dict[str, Any]]:
        paths = [str(image["path"]) for image in images]
        tag_stats = self.tags.stats(paths) if include_tags else {}
        results = []
        unresolved = []
        for image in images:
            path = str(image["path"])
            url = self.cache.cached_url(path) or ""
            thumb = self.cache.cached_thumb(path) or ""
            result = {"path": path, "size": int(image.get("size") or 0), "url": url, "thumbnail": thumb, "needs_url": not url}
            if include_tags:
                result["tags"] = tag_stats.get(path, {"likes": 0, "dislikes": 0, "categories": []})
            results.append(result)
            if not url:
                unresolved.append(image)
        if unresolved:
            self.prefetch_urls(unresolved)
        return results

    def prefetch_urls(self, images: list[dict[str, Any]]) -> None:
        client = OpenListClient(self.config)
        paths = [str(image["path"]) for image in images]

        def prefetch(path: str) -> None:
            try:
                self.cache.resolve(path, client)
            except Exception:
                pass

        threading.Thread(
            target=lambda: list(self.url_executor.map(prefetch, paths)),
            name="openlist-url-prefetch",
            daemon=True,
        ).start()

    def voter_id(self, ip: str, user_agent: str, admin_token: str | None) -> str:
        if self.config["tagging_scope"] == "token":
            if not admin_token:
                return ""
            return "t:" + hmac.new(b"openlist-tag-token", admin_token.encode("utf-8"), "sha256").hexdigest()[:16]
        raw = (ip or "") + "|" + (user_agent or "")
        return "a:" + hmac.new(b"openlist-tag-anon", raw.encode("utf-8"), "sha256").hexdigest()[:16]

    TRASH_TAG = "🗑️ 垃圾桶"

    def trash_paths(self) -> list[str]:
        return sorted(self.tags.paths_for_tag(self.TRASH_TAG))

    def delete_trash_images(self, paths: list[str] | None = None) -> dict[str, Any]:
        if paths is None:
            paths = self.trash_paths()
        if not paths:
            return {"deleted": 0, "failed": 0, "errors": []}
        client = OpenListClient(self.config)
        index = self.repository.load()
        indexed = {img.get("path") for img in index.get("images", []) if isinstance(img, dict)}
        deleted = 0
        failed = 0
        errors: list[dict[str, str]] = []
        for path in paths:
            normalized = normalize_directory(path)
            if normalized not in indexed:
                self.tags.reset_path(normalized)
                continue
            try:
                client.remove_file(normalized)
                self.tags.reset_path(normalized)
                deleted += 1
            except Exception as error:
                failed += 1
                errors.append({"path": normalized, "error": str(error)})
        return {"deleted": deleted, "failed": failed, "errors": errors}


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
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>图库</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;background:#10131a;color:#e7edf7;font:15px system-ui,sans-serif}header{position:sticky;z-index:2;top:0;display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:14px 18px;background:#10131af2;border-bottom:1px solid #293040}button,.button{background:#4b8cff;border:0;border-radius:7px;color:#fff;padding:9px 13px;cursor:pointer;text-decoration:none}button:disabled{opacity:.45;cursor:not-allowed}.meta{color:#a9b7cd;font-size:13px}.spacer{flex:1}.gallery{padding:18px}.gallery.waterfall{display:flex;align-items:flex-start;gap:var(--grid-gap,12px)}.waterfall-column{display:flex;min-width:0;flex:1;flex-direction:column;gap:var(--grid-gap,12px)}.card{position:relative;background:#171c27;border:1px solid #293040;border-radius:10px;overflow:hidden}.preview-button{display:block;width:100%;padding:0;border:0;border-radius:0;background:transparent}.card img{width:100%;display:block;max-height:82vh;object-fit:contain;background:#080a0f}.hidden{display:none!important}a{color:inherit}#empty{padding:40px;text-align:center;color:#a9b7cd}dialog{width:min(96vw,1500px);height:min(94vh,1000px);padding:0;border:1px solid #3a455b;border-radius:12px;background:#10131a;color:#e7edf7}dialog::backdrop{background:#000c}.lightbox-head,.lightbox-foot{display:flex;gap:12px;align-items:center;padding:10px 12px}.lightbox-head{justify-content:space-between}.lightbox-controls{display:flex;gap:8px}.lightbox-stage{height:calc(94vh - 126px);overflow:auto;background:#080a0f;display:grid;place-items:center}.lightbox-image{display:block;width:100%;height:100%;object-fit:contain;transform-origin:center;transition:transform .15s ease}.lightbox-meta{min-width:0;display:grid;gap:4px}.lightbox-caption,.lightbox-directory{margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.lightbox-directory{color:#a9b7cd;font-size:13px}.modal-backdrop{position:fixed;z-index:4;inset:0;background:#0008}#announcement-backdrop{position:fixed!important;z-index:1000!important;inset:0!important;background:#10131a!important;opacity:1!important;pointer-events:auto!important;animation:backdrop-in .25s ease both}.announcement{z-index:1001!important;pointer-events:auto!important}body.announcement-open{overflow:hidden}body.announcement-open>header,body.announcement-open>main,body.announcement-open>#maintenance{pointer-events:none!important;user-select:none}body.announcement-open>#announcement-backdrop,body.announcement-open>#announcement{display:block!important;visibility:visible!important}.preferences{position:fixed;z-index:5;top:50%;left:50%;width:min(92vw,430px);height:auto;padding:20px;border:1px solid #3a455b;border-radius:12px;background:#10131a;color:#e7edf7;transform:translate(-50%,-50%);box-shadow:0 1.5rem 2.2rem rgba(0,0,0,.28)}.preferences h2{margin-top:0}.preferences label{display:grid;gap:7px;margin:14px 0}.preferences select,.preferences input{padding:9px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff}.preferences-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:18px}.announcement{position:fixed;z-index:5;top:50%;left:50%;width:min(94vw,680px);height:auto;padding:0;border:0;border-radius:22px;background:linear-gradient(145deg,#fffdf8 0%,#fff 54%,#fff0e3 100%);color:#282828;box-shadow:0 1.5rem 3rem rgba(0,0,0,.38);overflow:hidden;transform:translate(-50%,-50%);animation:announcement-in .32s cubic-bezier(.2,.8,.2,1) both}.announcement.is-closing{animation:announcement-out .22s ease-in both}@keyframes backdrop-in{from{opacity:0}to{opacity:1}}@keyframes announcement-in{from{opacity:0;transform:translate(-50%,-46%) scale(.96)}to{opacity:1;transform:translate(-50%,-50%) scale(1)}}@keyframes announcement-out{from{opacity:1;transform:translate(-50%,-50%) scale(1)}to{opacity:0;transform:translate(-50%,-46%) scale(.97)}}.announcement-main{padding:26px 30px 12px}.announcement-title{position:relative;z-index:0;display:inline-block;margin:0 0 18px;font-size:21px}.announcement-title::after{content:'';position:absolute;z-index:-1;right:-3px;bottom:2px;left:-3px;height:14px;border-radius:4px;background:#fbeecd;transform:skewX(-15deg)}.announcement-content{margin:0;line-height:1.7}.announcement-content h1,.announcement-content h2,.announcement-content h3,.announcement-content h4{margin:16px 0 8px}.announcement-content p{margin:8px 0}.announcement-content code{padding:2px 5px;border-radius:4px;background:#f3f5f7}.announcement-content pre{overflow:auto;padding:12px;border-radius:8px;background:#f3f5f7}.announcement-content pre code{padding:0}.announcement-content a{color:#b63813}.announcement-footer{padding:12px 30px 28px;text-align:center;background:linear-gradient(170deg,#fff 0%,#fff 38%,#fbeecd 100%)}.announcement-actions{display:flex;justify-content:center;gap:10px;flex-wrap:wrap}.announcement-footer button{border-radius:50px;background:linear-gradient(to right,#ff711f,#e50914);box-shadow:0 10px 12px -4px rgba(229,9,20,.25)}.maintenance{max-width:520px;margin:13vh auto;padding:28px;border:1px solid #293040;border-radius:12px;background:#171c27;text-align:center}.maintenance details{text-align:left;margin-top:22px}.maintenance label{display:grid;gap:7px;margin:14px 0}.maintenance input{padding:9px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff;width:100%}.header-menu{position:fixed;z-index:6;top:54px;right:8px;width:160px;display:flex;flex-direction:column;gap:6px;padding:10px;background:#10131a;border:1px solid #3a455b;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.4)}#header-menu-backdrop{position:fixed;z-index:5;inset:0}#header-menu-toggle{display:none;position:fixed;z-index:7;top:8px;right:8px;width:38px;height:38px;padding:0;font-size:18px;border-radius:8px}#header-menu .button,#header-menu button{width:100%;text-align:center;padding:8px 10px}#header-menu a{display:block;padding:8px 10px;text-align:center;border-radius:7px;background:#4b8cff;color:#fff;text-decoration:none}@media(max-width:560px){header{padding:10px 56px 10px 12px}header>strong,header>.meta{font-size:14px}header .spacer{display:none}header>button,header>a{display:none}.gallery{padding:10px}.lightbox-image{height:calc(94vh - 126px)}#header-menu-toggle{display:block}.card-tags{max-height:56px;gap:3px;padding:5px 7px}.card-tags .tag-vote,.card-tags .tag-category{padding:3px 6px;font-size:10px}.tag-bar{padding:6px 10px;gap:4px;max-height:64px;overflow:hidden}.tag-bar-label{display:none}.tag-chip{padding:3px 8px;font-size:11px}}
</style>
<style>
.gallery.slideshow{display:grid;min-height:calc(100vh - 80px);place-items:center}.gallery.slideshow .card{max-width:min(96vw,1280px)}.gallery.waterfall .card{min-height:180px}.gallery.waterfall .card img{max-height:none;min-height:180px}.lightbox-stage{position:relative;overflow:hidden;cursor:grab;touch-action:none;overscroll-behavior:contain}.lightbox-stage.is-dragging{cursor:grabbing}.lightbox-image{width:100%;height:100%;max-width:none;max-height:none;pointer-events:none;user-select:none;will-change:transform}.announcement{display:flex;max-height:min(78vh,680px);flex-direction:column}.announcement-main{display:flex;min-height:0;flex:1;flex-direction:column}.announcement-content{min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;padding-right:8px}.announcement-footer{flex:0 0 auto}body.announcement-open>#announcement{display:flex!important}
@media(max-width:560px){.gallery.waterfall{gap:8px;padding:8px}.waterfall-column{gap:8px}.announcement{width:92vw;height:min(68vh,520px);max-height:68vh;border-radius:14px}.announcement-main{padding:16px 18px 8px}.announcement-title{margin-bottom:10px;font-size:18px}.announcement-content{padding-right:5px;line-height:1.55}.announcement-content h1,.announcement-content h2,.announcement-content h3,.announcement-content h4{margin:10px 0 6px}.announcement-footer{padding:8px 18px 14px}.announcement-footer .meta{margin:4px 0 8px}.announcement-footer button{padding:8px 12px}dialog{width:100vw;height:100dvh;max-width:none;max-height:none;border:0;border-radius:0}.lightbox-stage{height:calc(100dvh - 126px)}.lightbox-image{height:100%}.lightbox-head,.lightbox-foot{padding:8px}.lightbox-controls{gap:5px}.lightbox-controls button{padding:8px 10px}}
</style>
<style>
.gallery.slideshow{min-height:calc(100vh - 176px)}.lightbox-stage{cursor:default}.lightbox-stage.can-pan{cursor:grab}.lightbox-stage.can-pan.is-dragging{cursor:grabbing}.lightbox-image{width:auto;height:auto;object-fit:fill}.slide-history{padding:8px 18px 14px;border-top:1px solid #293040;background:#10131a}.slide-history-track{display:flex;gap:8px;overflow-x:auto;padding:2px 1px 6px;scrollbar-width:thin;scroll-snap-type:x proximity}.slide-thumbnail{position:relative;flex:0 0 76px;width:76px;height:58px;padding:0;overflow:hidden;border:1px solid #3a455b;border-radius:6px;background:#080a0f;scroll-snap-align:center}.slide-thumbnail img{display:block;width:100%;height:100%;object-fit:cover}.slide-thumbnail.active{border-color:#fff;box-shadow:0 0 0 2px #4b8cff}.slide-thumbnail-index{position:absolute;right:3px;bottom:3px;min-width:19px;padding:1px 4px;border-radius:4px;background:#000b;color:#fff;font-size:11px}.preferences .check{display:flex;align-items:center;gap:8px}.preferences .check input{width:auto;margin:0}
@media(max-width:560px){.gallery.slideshow{min-height:calc(100dvh - 190px)}.slide-history{padding:7px 8px 10px}.slide-history-track{gap:6px}.slide-thumbnail{flex-basis:62px;width:62px;height:48px}}
</style>
<style>
.slide-history-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:7px}.slide-history-title{color:#a9b7cd;font-size:13px}.slide-history-latest{padding:6px 10px;background:#39445a;font-size:13px}
#theme-fab{position:fixed;right:18px;bottom:18px;z-index:60;width:46px;height:46px;padding:0;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;background:#171c27;border:1px solid #3a455b;color:#cdd6e8;cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.35);transition:transform .15s ease,box-shadow .15s ease,border-color .15s ease}
#theme-fab:hover{transform:translateY(-2px);box-shadow:0 10px 24px rgba(0,0,0,.45);border-color:#4b8cff}
body.has-slide-history #theme-fab{bottom:132px}
body.theme-light #theme-fab{background:#fff;border-color:#c8d1e0;color:#3a4252;box-shadow:0 6px 18px rgba(30,40,60,.18)}
.card{transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}
@media(hover:hover){.card:hover{transform:translateY(-3px);box-shadow:0 12px 28px rgba(0,0,0,.4);border-color:#4b8cff}}
.preview-button{cursor:zoom-in}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:#39445a;border-radius:6px}::-webkit-scrollbar-thumb:hover{background:#4b5b78}::-webkit-scrollbar-track{background:transparent}
body.theme-light ::-webkit-scrollbar-thumb{background:#c8d1e0}
.preferences-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 16px}
.preferences-grid label{margin:10px 0}
.preferences .check{margin:12px 0}
@media(max-width:400px){.preferences-grid{grid-template-columns:1fr}}
.slide-nav{position:fixed;z-index:3;display:flex;align-items:center;justify-content:center;padding:0;border:1px solid #3a455b;background:#10131acc;color:#e7edf7;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);cursor:pointer;transition:transform .15s ease,border-color .15s ease}
.slide-nav.prev{left:10px;top:50%;width:42px;height:64px;font-size:30px;border-radius:12px;transform:translateY(-50%)}
.slide-nav.next{right:10px;top:50%;width:42px;height:64px;font-size:30px;border-radius:12px;transform:translateY(-50%)}
.slide-nav.pause{left:50%;bottom:24px;width:44px;height:44px;font-size:16px;border-radius:50%;transform:translateX(-50%)}
body.has-slide-history .slide-nav.pause{bottom:132px}
.slide-nav.prev:hover{transform:translateY(-50%) scale(1.07);border-color:#4b8cff}
.slide-nav.next:hover{transform:translateY(-50%) scale(1.07);border-color:#4b8cff}
.slide-nav.pause:hover{transform:translateX(-50%) scale(1.07);border-color:#4b8cff}
body.theme-light .slide-nav{background:#ffffffd9;border-color:#c8d1e0;color:#3a4252}
.gallery.slideshow{touch-action:pan-y}
@media(max-width:560px){.slide-nav.prev{left:6px;width:36px;height:56px;font-size:26px}.slide-nav.next{right:6px;width:36px;height:56px;font-size:26px}.slide-nav.pause{bottom:12px}body.has-slide-history .slide-nav.pause{bottom:112px}}
@media(max-width:760px){header{padding:10px 56px 10px 12px}header .spacer{display:none}header>button,header>a{display:none}#header-menu-toggle{display:block}}
.header-menu{opacity:0;visibility:hidden;transform:translateY(-10px);transition:opacity .22s ease,transform .22s ease,visibility .22s}
.header-menu.open{opacity:1;visibility:visible;transform:none}
#header-menu-backdrop{opacity:0;visibility:hidden;background:#0007;transition:opacity .22s ease,visibility .22s}
#header-menu-backdrop.open{opacity:1;visibility:visible}
@media(max-width:760px){.header-menu{left:0;right:0;width:auto;top:var(--menu-top,56px);border-top:0;border-radius:0 0 14px 14px;padding:12px 14px;box-shadow:0 14px 28px rgba(0,0,0,.45)}#header-menu .button,#header-menu button{padding:11px 12px}}
@media(max-width:560px){#theme-fab{right:12px;bottom:12px}body.has-slide-history #theme-fab{bottom:112px}.preferences{width:calc(100vw - 24px);max-height:86vh;overflow:auto}.lightbox-foot{flex-wrap:wrap}.lightbox-meta{flex:1 1 100%}}
</style>
<style>
.tag-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:8px 18px;border-bottom:1px solid #293040;background:#10131a;max-width:100%}.tag-bar-label{color:#a9b7cd;font-size:13px;margin-right:4px}.tag-chip{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border:1px solid #3a455b;border-radius:16px;background:#171c27;color:#cdd6e8;font-size:12px;cursor:pointer;transition:all .15s ease;user-select:none}.tag-chip:hover{border-color:#4b8cff;transform:translateY(-1px)}.tag-chip.active{background:#4b8cff;border-color:#4b8cff;color:#fff}.tag-chip-count{opacity:.7;font-size:11px}.tag-clear{padding:4px 10px;border:1px solid #3a455b;border-radius:16px;background:transparent;color:#a9b7cd;font-size:12px;cursor:pointer}.tag-clear:hover{color:#fff;border-color:#ff6b6b}.card-tags{position:absolute;bottom:0;left:0;right:0;display:flex;flex-wrap:wrap;gap:5px;padding:7px 10px;background:linear-gradient(transparent,rgba(0,0,0,.65));transition:opacity .2s ease;max-height:72px;overflow:hidden}@media(hover:hover){.card-tags{opacity:0}.card:hover .card-tags{opacity:1}.card.has-active-tag .card-tags{opacity:1}}.tag-vote{display:inline-flex;align-items:center;gap:4px;padding:4px 8px;border:1px solid #3a455b;border-radius:6px;background:rgba(255,255,255,.1);color:#fff;font-size:12px;cursor:pointer;transition:all .15s ease;user-select:none}.tag-vote:hover{border-color:#4b8cff;background:rgba(255,255,255,.16);transform:translateY(-1px)}.tag-vote.active{background:#ff4d6d;border-color:#ff4d6d;color:#fff}.tag-vote:disabled{opacity:.5;cursor:wait;transform:none}.tag-category{display:inline-flex;align-items:center;gap:3px;padding:4px 8px;border:1px solid #3a455b;border-radius:6px;background:rgba(255,255,255,.1);color:#fff;font-size:11px;cursor:pointer;transition:all .15s ease;user-select:none}.tag-category:hover{border-color:#4b8cff;background:rgba(255,255,255,.16);transform:translateY(-1px)}.tag-category.active{background:#4b8cff;border-color:#4b8cff;color:#fff}.tag-category.tag-trash{border-color:#5a3030;background:rgba(255,100,100,.12)}.tag-category.tag-trash:hover{border-color:#ff6b6b;background:rgba(255,100,100,.2);transform:translateY(-1px)}.tag-category.tag-trash.active{background:#ff4757;border-color:#ff4757;color:#fff}.tag-vote-count{font-weight:600;min-width:14px;text-align:center}
body.theme-light{color-scheme:light;background:#eef1f6;color:#1a2333}body.theme-light header{background:#eef1f6f2;border-bottom-color:#dde3ee}body.theme-light .meta{color:#6b7689}body.theme-light .gallery{background:transparent}body.theme-light .card{background:#fff;border-color:#dde3ee}body.theme-light .card img{background:#f0f3f8}body.theme-light .tag-bar{background:#eef1f6;border-bottom-color:#dde3ee}body.theme-light .tag-chip{background:#fff;border-color:#c8d1e0;color:#3a4252}body.theme-light .tag-chip.active{background:#4b8cff;border-color:#4b8cff;color:#fff}body.theme-light .tag-clear{background:transparent;border-color:#c8d1e0;color:#6b7689}body.theme-light .preferences{background:#fff;color:#1a2333;border-color:#c8d1e0}body.theme-light .preferences select,body.theme-light .preferences input{background:#f7f9fc;border-color:#c8d1e0;color:#1a2333}body.theme-light .header-menu{background:#fff;border-color:#c8d1e0}body.theme-light .slide-history{background:#eef1f6;border-top-color:#dde3ee}body.theme-light .slide-history-title{color:#6b7689}body.theme-light .slide-thumbnail{background:#f0f3f8;border-color:#dde3ee}body.theme-light .slide-thumbnail.active{border-color:#4b8cff}body.theme-light .maintenance{background:#fff;border-color:#dde3ee}body.theme-light .maintenance input{background:#f7f9fc;border-color:#c8d1e0;color:#1a2333}
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
<button id="header-menu-toggle" type="button" aria-label="菜单" aria-expanded="false">☰</button>
<div id="header-menu-backdrop"></div>
<aside id="header-menu" class="header-menu" aria-label="快捷菜单">
  <button id="menu-previous" class="hidden" type="button">← 上一张</button>
  <button id="menu-slideshow-toggle" class="hidden" type="button" aria-pressed="false">暂停</button>
  <button id="menu-next" class="hidden" type="button">下一张 →</button>
  <button id="menu-refresh" type="button">刷新</button>
  <button id="menu-settings" type="button">显示设置</button>
  <button id="menu-announcement" class="hidden" type="button">公告</button>
  <a href="/admin" class="button">管理</a>
</aside>
<div id="tag-bar" class="tag-bar hidden"></div>
<main id="gallery" class="gallery"></main>
<button id="slide-nav-prev" class="slide-nav prev hidden" type="button" aria-label="上一张">‹</button>
<button id="slide-nav-next" class="slide-nav next hidden" type="button" aria-label="下一张">›</button>
<button id="slide-nav-pause" class="slide-nav pause hidden" type="button" aria-label="暂停播放">⏸</button>
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
  <div class="preferences-grid">
    <label>视图<select id="layout-mode"><option value="slideshow">幻灯片</option><option value="waterfall">瀑布流</option></select></label>
    <label>图片名称样式<select id="caption-mode"><option value="path">完整路径</option><option value="name">仅图片名称</option><option value="hidden">不展示</option></select></label>
    <label>图片画质<select id="preview-quality"><option value="176">极速（176px）</option><option value="480">清晰（480px）</option><option value="800">高清（800px）</option><option value="1280">超清（1280px）</option><option value="2560">极清（2560px）</option></select></label>
    <label>大图画质<select id="lightbox-quality"><option value="original">原图（最清晰）</option><option value="2560">极清（2560px，推荐）</option><option value="1280">超清（1280px，最快）</option></select></label>
    <label>标签筛选模式<select id="filter-mode"><option value="union">并集（任一匹配）</option><option value="intersect">交集（全部匹配）</option></select></label>
    <label class="slideshow-only">自动播放间隔（秒，0 关闭）<input id="slideshow-interval" type="number" min="0" max="300" step="1"></label>
  </div>
  <label class="check"><input id="show-tags-enabled" type="checkbox">在图片上显示标签按钮（点赞/分类）</label>
  <p class="meta">图片画质与大图画质越高越清晰，流量消耗也越大；设置仅保存在当前浏览器。</p>
  <div class="preferences-actions"><button id="preferences-reset" type="button">恢复默认</button><button id="preferences-save" type="button">保存</button><button id="preferences-close" type="button">关闭</button></div>
</section>
<div id="announcement-backdrop" class="modal-backdrop announcement-backdrop hidden"></div>
<section id="announcement" class="announcement hidden" role="dialog" aria-modal="true" aria-labelledby="announcement-title">
  <div class="announcement-main"><h2 id="announcement-title" class="announcement-title"></h2><div id="announcement-content" class="announcement-content"></div></div>
  <div class="announcement-footer"><p id="announcement-reading" class="meta"></p><div class="announcement-actions"><button id="announcement-close-once" type="button">本次关闭</button><button id="announcement-close-forever" type="button">不再显示</button></div></div>
</section>
<button id="theme-fab" type="button" title="切换明暗主题" aria-label="切换明暗主题">🌙</button>
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
const captionMode=document.querySelector('#caption-mode');
const previewQuality=document.querySelector('#preview-quality');
const lightboxQuality=document.querySelector('#lightbox-quality');
const themeFab=document.querySelector('#theme-fab');
const filterMode=document.querySelector('#filter-mode');
const showTagsEnabled=document.querySelector('#show-tags-enabled');
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
let slideExhausted=false;
let slideTimer=null;
let slideshowPaused=false;
let slideHistory=[];
let taggingConfig=null;
let activeTagFilters=[];
let tagCategoriesCache={};
let slideHistorySequence=0;
let waterfallLoading=false;
let waterfallExhausted=false;
let waterfallPrefetch=[];
let waterfallPrefetching=false;
let waterfallPrefetchToken=0;
let loadedCount=0;
let cardSequence=0;
let waterfallColumnCount=0;
let resizeTimer=null;
const slidePreloads=new Map();
const activePointers=new Map();
const SLIDE_PRELOAD_COUNT=3;
const SLIDE_INITIAL_LOAD=6;
const WATERFALL_BATCH_SIZE=20;
let dragStart=null;
let pinchStart=null;
let lightboxBaseWidth=0;
let lightboxBaseHeight=0;
let lastHiddenAt=0;
let recoveryPromise=null;
const URL_REFRESH_AGE_MS=25*60*1000;
const IDLE_RECOVERY_MS=5*60*1000;
const PREVIEW_QUALITY_OPTIONS=['176','480','800','1280','2560'];
const LIGHTBOX_QUALITY_OPTIONS=['original','2560','1280'];
function sizedThumb(url,size){
  if(!url) return '';
  if(/[?&]width=\d+/.test(url)) return url.replace(/([?&]width=)\d+/,'$1'+size).replace(/([?&]height=)\d+/,'$1'+size);
  return url;
}
function cardSrc(image){
  if(!image) return '';
  if(!image.thumbnail) return image.url||'';
  return sizedThumb(image.thumbnail,Number(settings&&settings.preview_quality)||176);
}
function lightboxSrc(image){
  if(!image) return '';
  const quality=settings&&settings.lightbox_quality||'original';
  if(quality==='original'||!image.thumbnail) return image.url||'';
  return sizedThumb(image.thumbnail,quality==='1280'?1280:2560);
}

function normalizedPreferences(value,defaults){
  const stored=value&&typeof value==='object'?value:{};
  const defaultLayout=defaults.view_layout==='waterfall'?'waterfall':'slideshow';
  const storedLayout=['single','grid'].includes(stored.view_layout)?'slideshow':stored.view_layout;
  const result={view_layout:storedLayout,slideshow_interval:stored.slideshow_interval,grid_gap:stored.grid_gap,caption_mode:stored.caption_mode,show_tags_enabled:stored.show_tags_enabled,theme:stored.theme,filter_mode:stored.filter_mode,preview_quality:stored.preview_quality,lightbox_quality:stored.lightbox_quality};
  if(!['slideshow','waterfall'].includes(result.view_layout)) result.view_layout=defaultLayout;
  if(!['path','name','hidden'].includes(result.caption_mode)) result.caption_mode=defaults.caption_mode;
  result.slideshow_interval=Math.max(0,Math.min(300,Number(stored.slideshow_interval??8)||0));
  result.grid_gap=Math.max(0,Math.min(48,Number(stored.grid_gap??defaults.grid_gap)||0));
  result.show_tags_enabled=result.show_tags_enabled!==false;
  if(!['dark','light'].includes(result.theme)) result.theme=['dark','light'].includes(defaults.theme)?defaults.theme:'dark';
  if(!['union','intersect'].includes(result.filter_mode)) result.filter_mode='union';
  if(!PREVIEW_QUALITY_OPTIONS.includes(result.preview_quality)) result.preview_quality='176';
  if(!LIGHTBOX_QUALITY_OPTIONS.includes(result.lightbox_quality)) result.lightbox_quality='original';
  return result;
}

async function loadSettings(){
  const response=await fetch('/api/public-config',{cache:'no-store'});
  if(!response.ok) throw new Error('无法读取浏览设置');
  const defaults=await response.json();
  let stored={};
  try{stored=JSON.parse(localStorage.getItem(PREFERENCE_KEY)||'{}');}catch(error){localStorage.removeItem(PREFERENCE_KEY);}
  const preferences=normalizedPreferences(stored,defaults);
  preferences.announcement=defaults.announcement;
  preferences.maintenance_enabled=defaults.maintenance_enabled;
  preferences.directory_display_enabled=defaults.directory_display_enabled;
  preferences.directory_display_depth=defaults.directory_display_depth;
  preferences.filter_enabled=defaults.filter_enabled!==false;
  preferences.tagging=defaults.tagging||{enabled:false,scope:'disabled',categories:[],allow_custom:false,sort_default:'likes'};
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
  return escapeHtml(value).replace(/&lt;font\s+color=(?:&quot;|&#39;)?(#[0-9a-f]{3,8}|[a-z]+)(?:&quot;|&#39;)?\s*&gt;/gi,'<span style="color:$1">').replace(/&lt;\/font&gt;/gi,'</span>').replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>').replace(/^#### (.*)$/gm,'<h4>$1</h4>').replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>').replace(/\n\n/g,'</p><p>').replace(/\n/g,'<br>');
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

function persistPreferences(){localStorage.setItem(PREFERENCE_KEY,JSON.stringify({view_layout:settings.view_layout,slideshow_interval:settings.slideshow_interval,grid_gap:settings.grid_gap,caption_mode:settings.caption_mode,show_tags_enabled:settings.show_tags_enabled,theme:settings.theme,filter_mode:settings.filter_mode,preview_quality:settings.preview_quality,lightbox_quality:settings.lightbox_quality}));}

function openPreferences(){
  clearSlideTimer();
  previewQuality.value=settings.preview_quality;
  lightboxQuality.value=settings.lightbox_quality;
  layoutMode.value=settings.view_layout;
  slideshowInterval.value=settings.slideshow_interval;
  syncSlideshowOption();
  captionMode.value=settings.caption_mode;
  filterMode.value=settings.filter_mode;
  showTagsEnabled.checked=settings.show_tags_enabled;
  preferencesPanel.classList.remove('hidden');
  preferencesBackdrop.classList.remove('hidden');
}

function closePreferences(){preferencesPanel.classList.add('hidden');preferencesBackdrop.classList.add('hidden');scheduleSlideshow();}

function syncSlideshowOption(){document.querySelector('.slideshow-only').classList.toggle('hidden',layoutMode.value!=='slideshow');}
layoutMode.addEventListener('change',syncSlideshowOption);

function applyGalleryTheme(theme){document.body.classList.toggle('theme-light',theme==='light');document.body.classList.toggle('theme-dark',theme!=='light');if(themeFab)themeFab.textContent=theme==='light'?'☀':'🌙';}

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
    const timeout=setTimeout(()=>controller.abort(),45000);
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

function applyResolvedUrl(image,data){
  if(!data||data.error||!data.url) return image;
  image.url=data.url;
  if(data.thumbnail) image.thumbnail=data.thumbnail;
  image._resolvedAt=Date.now();
  image.needs_url=false;
  return image;
}

async function refreshImageUrls(images,force=false){
  const pending=images.filter(image=>image&&image.path&&(force||image.needs_url||!image.url||Date.now()-(image._resolvedAt||0)>URL_REFRESH_AGE_MS));
  if(!pending.length) return images;
  const data=await fetchJsonWithRetry('/api/download-url',{
    method:'POST',
    headers:{'Content-Type':'application/json',...adminHeaders()},
    body:JSON.stringify({paths:pending.map(image=>image.path),fresh:!!force})
  },2);
  const lookup=new Map((data.images||[]).map(item=>[item.path,item]));
  const failed=[];
  for(const image of pending){
    const resolved=lookup.get(image.path);
    if(resolved&&resolved.url&&!resolved.error){
      applyResolvedUrl(image,resolved);
    }else{
      failed.push(image);
    }
  }
  if(failed.length){
    await Promise.all(failed.map(image=>refreshImageUrl(image,force).catch(()=>{})));
  }
  return images;
}

async function refreshImageUrl(image,force=true){
  const fresh=force?'&fresh=1':'';
  const data=await fetchJsonWithRetry('/api/download-url?path='+encodeURIComponent(image.path)+fresh,{headers:adminHeaders()},2);
  return applyResolvedUrl(image,data);
}

async function ensureFreshImage(image){
  try{
    if(image.needs_url||!image.url) await refreshImageUrl(image,false);
    else if(!image._resolvedAt||Date.now()-image._resolvedAt>URL_REFRESH_AGE_MS) await refreshImageUrl(image,false);
  }catch(error){
    image.needs_url=false;
  }
  return image;
}

function attachImageRecovery(element,image){
  element.addEventListener('load',()=>{delete element.dataset.refreshAttempted;},{once:false});
  element.addEventListener('error',()=>{
    if(element.dataset.refreshAttempted==='1') return;
    element.dataset.refreshAttempted='1';
    refreshImageUrl(image,true).then(()=>{element.src=cardSrc(image);}).catch(()=>{});
  });
}

async function downloadImage(){
  if(!activeImage) return;
  if(!activeImage.url||activeImage.needs_url||!activeImage._resolvedAt||Date.now()-activeImage._resolvedAt>URL_REFRESH_AGE_MS){
    await refreshImageUrl(activeImage,false);
  }
  const link=document.createElement('a');
  link.href=activeImage.url;
  link.download=imageName(activeImage.path)||'';
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
  const directory=settings.caption_mode!=='path'?directoryFor(image):'';
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
  lightboxImage.alt=imageName(image.path)||'';
  lightboxImage.src=lightboxSrc(image);
  if(lightboxImage.complete&&lightboxImage.naturalWidth) fitLightboxImage(false);
}

function createCard(image,eager=false){
  const card=document.createElement('article');
  card.className='card';
  card.dataset.sequence=String(cardSequence++);
  card.dataset.path=image.path||'';
  const preview=document.createElement('button');
  preview.className='preview-button';
  preview.type='button';
  preview.setAttribute('aria-label','查看图片');
  preview.onclick=()=>openLightbox(image);
  const picture=document.createElement('img');
  picture.loading=eager?'eager':'lazy';
  picture.decoding='async';
  if(eager) picture.fetchPriority='high';
  picture.alt='';
  attachImageRecovery(picture,image);
  const applySrc=()=>{
    if(picture.dataset.srcApplied==='1') return card._ready||Promise.resolve();
    picture.dataset.srcApplied='1';
    if(image.needs_url||!image.url){
      card._ready=ensureFreshImage(image).then(()=>{picture.src=cardSrc(image);}).catch(()=>{});
    }else{
      picture.src=cardSrc(image);
      card._ready=Promise.resolve();
    }
    return card._ready;
  };
  card._applySrc=applySrc;
  if(eager) applySrc();
  else card._ready=Promise.resolve();
  preview.append(picture);
  card.append(preview);
  if(taggingConfig&&taggingConfig.enabled&&image.tags&&settings.show_tags_enabled){
    const tags=document.createElement('div');
    tags.className='card-tags';
    const like=document.createElement('button');
    like.className='tag-vote like';
    like.type='button';
    like.innerHTML='❤ <span class="tag-vote-count">'+(image.tags.likes||0)+'</span>';
    like.onclick=(e)=>{e.stopPropagation();handleTagVote(image.path,'like',like,e);};
    tags.append(like);
    if(taggingConfig.categories&&taggingConfig.categories.length){
      taggingConfig.categories.forEach(cat=>{
        const btn=document.createElement('button');
        btn.className='tag-category';
        btn.type='button';
        btn.textContent=cat;
        btn.dataset.category=cat;
        if(image.tags.categories&&image.tags.categories.includes(cat))btn.classList.add('active');
        btn.onclick=(e)=>{e.stopPropagation();handleTagCategory(image.path,cat,btn,e);};
        tags.append(btn);
      });
    }
    if(taggingConfig.trash_tag){
      const trash=document.createElement('button');
      trash.className='tag-category tag-trash';
      trash.type='button';
      trash.textContent='🗑️';
      trash.title='标记为垃圾图片';
      trash.dataset.category=taggingConfig.trash_tag;
      if(image.tags.categories&&image.tags.categories.includes(taggingConfig.trash_tag))trash.classList.add('active');
      trash.onclick=(e)=>{e.stopPropagation();handleTagCategory(image.path,taggingConfig.trash_tag,trash,e);};
      tags.append(trash);
    }
    if((image.tags.likes||0)>0||(image.tags.categories&&image.tags.categories.length>0)){card.classList.add('has-active-tag');}
    card.append(tags);
  }
  return card;
}

async function requestImages(count){
  let url='/api/images/random?count='+count;
  if(activeTagFilters.length){
    activeTagFilters.forEach(t=>{url+='&tag='+encodeURIComponent(t);});
    if(settings.filter_mode==='intersect')url+='&filter_mode=intersect';
  }
  url+='&_='+Date.now();
  const data=await fetchJsonWithRetry(url,{headers:adminHeaders()},3);
  const resolvedAt=Date.now();
  return data.images.map(image=>({...image,_resolvedAt:resolvedAt}));
}

async function handleTagVote(path,type,button,event){
  if(!taggingConfig||!taggingConfig.enabled)return;
  const isActive=button.classList.contains('active');
  const newValue=!isActive;
  button.disabled=true;
  try{
    const response=await fetch('/api/tagging/vote',{method:'POST',headers:{'Content-Type':'application/json',...adminHeaders()},body:JSON.stringify({path:path,type:type,value:newValue})});
    if(!response.ok){const err=await response.json().catch(()=>({}));throw new Error(err.error||'投票失败');}
    const result=await response.json();
    button.classList.toggle('active',newValue);
    const count=button.querySelector('.tag-vote-count');
    if(count)count.textContent=result.likes!==undefined?result.likes:count.textContent;
    const card=button.closest('.card');
    if(card)updateCardActiveTag(card);
  }catch(error){
    statusEl.textContent='标签操作失败：'+error.message;
  }finally{
    button.disabled=false;
  }
}

async function handleTagCategory(path,category,button,event){
  if(!taggingConfig||!taggingConfig.enabled)return;
  const isActive=button.classList.contains('active');
  const newValue=!isActive;
  button.disabled=true;
  try{
    const response=await fetch('/api/tagging/vote',{method:'POST',headers:{'Content-Type':'application/json',...adminHeaders()},body:JSON.stringify({path:path,type:'category',category:category,value:newValue})});
    if(!response.ok){const err=await response.json().catch(()=>({}));throw new Error(err.error||'分类标记失败');}
    button.classList.toggle('active',newValue);
    const card=button.closest('.card');
    if(card)updateCardActiveTag(card);
  }catch(error){
    statusEl.textContent='标签操作失败：'+error.message;
  }finally{
    button.disabled=false;
  }
}

function updateCardActiveTag(card){
  const hasActive=card.querySelector('.tag-vote.active,.tag-category.active');
  card.classList.toggle('has-active-tag',!!hasActive);
}

async function loadTagCategories(){
  if(!taggingConfig||!taggingConfig.enabled)return;
  try{
    const response=await fetch('/api/tagging/categories',{cache:'no-store'});
    if(response.ok){tagCategoriesCache=await response.json();}
  }catch(error){}
  renderTagBar();
}

function renderTagBar(){
  const bar=document.querySelector('#tag-bar');
  if(!taggingConfig||!taggingConfig.enabled||settings.filter_enabled===false){bar.classList.add('hidden');activeTagFilters=[];return;}
  const allCats=taggingConfig.categories||[];
  bar.innerHTML='';
  const label=document.createElement('span');
  label.className='tag-bar-label';
  label.textContent='标签筛选：';
  bar.append(label);
  allCats.forEach(cat=>{
    const chip=document.createElement('button');
    chip.className='tag-chip';
    chip.type='button';
    chip.textContent=cat;
    const count=tagCategoriesCache.categories&&tagCategoriesCache.categories[cat];
    if(count){const c=document.createElement('span');c.className='tag-chip-count';c.textContent='('+count+')';chip.append(c);}
    if(activeTagFilters.includes(cat))chip.classList.add('active');
    chip.onclick=()=>{
      const idx=activeTagFilters.indexOf(cat);
      if(idx>=0)activeTagFilters.splice(idx,1);else activeTagFilters.push(cat);
      renderTagBar();
      render().catch(showError);
    };
    bar.append(chip);
  });
  if(taggingConfig.trash_tag){
    const chip=document.createElement('button');
    chip.className='tag-chip';
    chip.type='button';
    chip.textContent='🗑️';
    chip.title=taggingConfig.trash_tag;
    const count=tagCategoriesCache.categories&&tagCategoriesCache.categories[taggingConfig.trash_tag];
    if(count){const c=document.createElement('span');c.className='tag-chip-count';c.textContent='('+count+')';chip.append(c);}
    if(activeTagFilters.includes(taggingConfig.trash_tag))chip.classList.add('active');
    chip.onclick=()=>{
      const idx=activeTagFilters.indexOf(taggingConfig.trash_tag);
      if(idx>=0)activeTagFilters.splice(idx,1);else activeTagFilters.push(taggingConfig.trash_tag);
      renderTagBar();
      render().catch(showError);
    };
    bar.append(chip);
  }
  if(activeTagFilters.length){
    const clear=document.createElement('button');
    clear.className='tag-clear';
    clear.type='button';
    clear.textContent='清除筛选';
    clear.onclick=()=>{activeTagFilters=[];renderTagBar();render().catch(showError);};
    bar.append(clear);
  }
  bar.classList.remove('hidden');
}

function preferredWaterfallColumns(){
  const width=gallery.clientWidth||window.innerWidth;
  return width>900?3:2;
}

function waterfallGap(){
  return Number(settings&&settings.grid_gap)||12;
}

function estimatedCardHeight(card){
  const picture=card.querySelector('img');
  const columns=Math.max(1,waterfallColumnCount||preferredWaterfallColumns());
  const columnWidth=Math.max(1,((gallery.clientWidth||window.innerWidth)-(columns-1)*waterfallGap())/columns);
  if(picture&&picture.naturalWidth&&picture.naturalHeight) return columnWidth*picture.naturalHeight/picture.naturalWidth+waterfallGap();
  return 180+waterfallGap();
}

function columnEstimateHeight(column){
  return [...column.children].reduce((sum,card)=>sum+estimatedCardHeight(card),0);
}

function shortestWaterfallColumn(){
  const columns=[...gallery.querySelectorAll('.waterfall-column')];
  if(!columns.length) return null;
  return columns.reduce((shortest,column)=>columnEstimateHeight(column)<columnEstimateHeight(shortest)?column:shortest);
}

function revealWaterfallCard(card,priority){
  waterfallRevealObserver.unobserve(card);
  const picture=card.querySelector('img');
  if(!picture) return;
  if(priority==='high'){picture.loading='eager';picture.fetchPriority='high';}
  else if(priority==='auto'){picture.loading='eager';picture.fetchPriority='auto';}
  if(card._applySrc) card._applySrc();
}

const waterfallRevealObserver=new IntersectionObserver((entries)=>{
  entries.forEach(entry=>{
    if(!entry.isIntersecting) return;
    revealWaterfallCard(entry.target);
  });
},{rootMargin:'100% 0px',threshold:0.01});

function prioritizeWaterfallImages(cards){
  const limit=window.innerHeight*1.25;
  const items=[...(cards||gallery.querySelectorAll('.card'))].map(card=>{
    const rect=card.getBoundingClientRect();
    return {card,top:rect.top,left:rect.left};
  }).sort((left,right)=>left.top-right.top||left.left-right.left);
  items.forEach((item,index)=>{
    const picture=item.card.querySelector('img');
    if(picture&&picture.dataset.srcApplied==='1') return;
    if(item.top<limit) revealWaterfallCard(item.card,index<6?'high':'auto');
    else waterfallRevealObserver.observe(item.card);
  });
}

function appendWaterfallCard(card){
  const column=shortestWaterfallColumn();
  if(column) column.append(card);
}

function setupWaterfallColumns(){
  const count=preferredWaterfallColumns();
  const cards=[...gallery.querySelectorAll('.card')].sort((left,right)=>Number(left.dataset.sequence)-Number(right.dataset.sequence));
  waterfallColumnCount=count;
  gallery.replaceChildren(...Array.from({length:count},()=>{const column=document.createElement('div');column.className='waterfall-column';return column;}));
  cards.forEach(appendWaterfallCard);
  prioritizeWaterfallImages(cards);
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
  const menuToggle=document.querySelector('#menu-slideshow-toggle');
  if(menuToggle){menuToggle.textContent=slideshowPaused?'继续':'暂停';menuToggle.setAttribute('aria-pressed',String(slideshowPaused));}
  const navPause=document.querySelector('#slide-nav-pause');
  if(navPause){navPause.textContent=slideshowPaused?'▶':'⏸';navPause.setAttribute('aria-label',slideshowPaused?'继续播放':'暂停播放');}
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
  if(slideExhausted) return [];
  if(slideLoadPromise) return slideLoadPromise;
  slideLoadPromise=requestImages(count).then(images=>{
    slideImages.push(...images);
    if(images.length<count) slideExhausted=true;
    return images;
  }).catch(error=>{
    slideExhausted=true;
    throw error;
  }).finally(()=>{slideLoadPromise=null;});
  return slideLoadPromise;
}

async function ensureSlideBuffer(){
  const remaining=slideImages.length-slideIndex-1;
  if(remaining<SLIDE_PRELOAD_COUNT&&!slideExhausted) await appendSlideImages(SLIDE_PRELOAD_COUNT-remaining);
  const upcoming=slideImages.slice(slideIndex,slideIndex+1+SLIDE_PRELOAD_COUNT);
  await refreshImageUrls(upcoming,false).catch(()=>{});
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
    thumbnail.src=cardSrc(image);
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
    empty.textContent=activeTagFilters.length?'没有匹配的图片':'没有可用图片';
    gallery.append(empty);
    statusEl.textContent=activeTagFilters.length?'筛选无结果':'暂无图片';
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
    slideExhausted=false;
    slideHistory=[];
    slideHistorySequence=0;
    slidePreloads.clear();
    await appendSlideImages(SLIDE_INITIAL_LOAD);
  }
  await ensureSlideBuffer();
  renderSlideshow();
}

async function nextSlide(){
  clearSlideTimer();
  await ensureSlideBuffer();
  if(slideIndex<slideImages.length-1){
    slideIndex+=1;
  }else if(slideExhausted){
    setSlideshowPaused(true);
  }
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

function clearWaterfallPrefetch(){
  waterfallPrefetch=[];
  waterfallPrefetching=false;
  waterfallPrefetchToken+=1;
}

async function prefetchNextWaterfallBatch(){
  if(waterfallPrefetching||waterfallExhausted||waterfallPrefetch.length) return;
  const token=waterfallPrefetchToken;
  waterfallPrefetching=true;
  try{
    const images=await requestImages(WATERFALL_BATCH_SIZE);
    if(token!==waterfallPrefetchToken) return;
    if(!images.length){
      waterfallExhausted=true;
      return;
    }
    await refreshImageUrls(images,false).catch(()=>{});
    if(token!==waterfallPrefetchToken) return;
    waterfallPrefetch=images;
  }catch(error){
    if(token===waterfallPrefetchToken) waterfallPrefetch=[];
  }finally{
    if(token===waterfallPrefetchToken) waterfallPrefetching=false;
  }
}

async function loadWaterfallBatch(reset){
  if(waterfallLoading) return;
  if(!reset&&waterfallExhausted) return;
  waterfallLoading=true;
  refreshButton.disabled=true;
  try{
    if(reset){
      clearWaterfallPrefetch();
      waterfallRevealObserver.disconnect();
      loadedCount=0;
      cardSequence=0;
      waterfallExhausted=false;
      gallery.replaceChildren();
      setupWaterfallColumns();
    }
    let images=waterfallPrefetch.length?waterfallPrefetch.splice(0,waterfallPrefetch.length):await requestImages(WATERFALL_BATCH_SIZE);
    await refreshImageUrls(images,false).catch(()=>{});
    if(!images.length){
      waterfallExhausted=true;
      if(loadedCount===0){
        gallery.replaceChildren();
        const empty=document.createElement('p');
        empty.id='empty';
        empty.textContent=activeTagFilters.length?'没有匹配的图片':'没有可用图片';
        gallery.append(empty);
        statusEl.textContent=activeTagFilters.length?'筛选无结果':'暂无图片';
      }else{
        statusEl.textContent='瀑布流 · 已加载 '+loadedCount+' 张（已全部加载）';
      }
    }else{
      const cards=images.map(image=>createCard(image));
      cards.forEach(appendWaterfallCard);
      prioritizeWaterfallImages(cards);
      loadedCount+=images.length;
      if(images.length<WATERFALL_BATCH_SIZE) waterfallExhausted=true;
      statusEl.textContent='瀑布流 · 已加载 '+loadedCount+' 张';
      await Promise.allSettled(cards.map(card=>card._ready||Promise.resolve()));
      prefetchNextWaterfallBatch();
    }
  }catch(error){
    if(loadedCount===0){
      gallery.replaceChildren();
      const empty=document.createElement('p');
      empty.id='empty';
      empty.textContent=activeTagFilters.length?'没有匹配的图片':'加载失败';
      gallery.append(empty);
      statusEl.textContent=activeTagFilters.length?'筛选无结果':'加载失败：'+error.message;
      waterfallExhausted=true;
    }else{
      statusEl.textContent='瀑布流 · 已加载 '+loadedCount+' 张';
    }
  }finally{
    waterfallLoading=false;
    refreshButton.disabled=false;
    maybeLoadMoreWaterfall();
  }
}

function maybeLoadMoreWaterfall(){
  if(!settings||settings.view_layout!=='waterfall'||waterfallExhausted||waterfallLoading) return;
  if(window.scrollY+window.innerHeight>=document.documentElement.scrollHeight*.6){
    loadWaterfallBatch(false).catch(showError);
  }
}

async function recoverAfterIdle(){
  if(recoveryPromise) return recoveryPromise;
  if(!settings||settings.maintenance_enabled&&!maintenanceAccessToken) return;
  recoveryPromise=(async()=>{
    try{
      if(settings.view_layout==='slideshow'){
        clearSlideTimer();
        statusEl.textContent='正在恢复图片连接…';
        const current=slideImages[slideIndex];
        if(current){
          try{await refreshImageUrl(current,true);}
          catch(error){
            try{
              const replacement=await requestImages(1);
              slideImages[slideIndex]=replacement[0];
            }catch(e){
              slideImages=[];
              slideIndex=0;
              slideExhausted=false;
              await ensureSlideBuffer();
            }
          }
        }
        slidePreloads.clear();
        await ensureSlideBuffer().catch(()=>{});
        renderSlideshow();
      }else{
        await loadWaterfallBatch(false).catch(()=>{});
      }
      scheduleSlideshow();
    }catch(error){
      showError(error);
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
  const slideshowVisible=!restricted&&settings.view_layout==='slideshow';
  document.querySelector('#slide-nav-prev').classList.toggle('hidden',!slideshowVisible);
  document.querySelector('#slide-nav-next').classList.toggle('hidden',!slideshowVisible);
  document.querySelector('#slide-nav-pause').classList.toggle('hidden',!slideshowVisible);
  document.querySelector('#menu-previous').classList.toggle('hidden',!slideshowVisible);
  document.querySelector('#menu-next').classList.toggle('hidden',!slideshowVisible);
  document.querySelector('#menu-slideshow-toggle').classList.toggle('hidden',!slideshowVisible);
  slideHistoryPanel.classList.toggle('hidden',restricted||settings.view_layout!=='slideshow');
  document.body.classList.toggle('has-slide-history',!restricted&&settings.view_layout==='slideshow');
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
  if(settings&&settings.view_layout==='slideshow'&&!slideshowPaused&&activeTagFilters.length===0) scheduleSlideshow(15000);
}

previousButton.onclick=previousSlide;
nextButton.onclick=()=>nextSlide().catch(showError);
document.querySelector('#slide-nav-prev').onclick=previousSlide;
document.querySelector('#slide-nav-next').onclick=()=>nextSlide().catch(showError);
document.querySelector('#slide-nav-pause').onclick=()=>setSlideshowPaused(!slideshowPaused);
let touchSwipeStart=null;
gallery.addEventListener('pointerdown',event=>{if(event.pointerType==='mouse')return;touchSwipeStart={x:event.clientX,y:event.clientY};});
gallery.addEventListener('pointerup',event=>{
  if(!touchSwipeStart)return;
  const dx=event.clientX-touchSwipeStart.x,dy=event.clientY-touchSwipeStart.y;
  touchSwipeStart=null;
  if(!settings||settings.view_layout!=='slideshow')return;
  if(Math.abs(dx)>48&&Math.abs(dx)>Math.abs(dy)*1.4){dx>0?previousSlide():nextSlide().catch(showError);}
});
gallery.addEventListener('pointercancel',()=>{touchSwipeStart=null;});
slideshowToggle.onclick=()=>setSlideshowPaused(!slideshowPaused);
slideHistoryLatest.onclick=()=>{const latest=slideHistory[slideHistory.length-1];if(latest)showHistoryImage(latest).catch(showError);};
refreshButton.onclick=()=>render().catch(showError);
settingsButton.onclick=openPreferences;
announcementButton.onclick=()=>showAnnouncement(true);
const headerMenuToggle=document.querySelector('#header-menu-toggle');
const headerMenu=document.querySelector('#header-menu');
const headerMenuBackdrop=document.querySelector('#header-menu-backdrop');
function openHeaderMenu(){if(headerMenu.classList.contains('open')){closeHeaderMenu();return;}clearSlideTimer();headerMenu.style.setProperty('--menu-top',Math.round(document.querySelector('header').getBoundingClientRect().bottom)+'px');headerMenu.classList.add('open');headerMenuBackdrop.classList.add('open');headerMenuToggle.setAttribute('aria-expanded','true');}
function closeHeaderMenu(){headerMenu.classList.remove('open');headerMenuBackdrop.classList.remove('open');headerMenuToggle.setAttribute('aria-expanded','false');scheduleSlideshow();}
headerMenuToggle.onclick=openHeaderMenu;
headerMenuBackdrop.onclick=closeHeaderMenu;
document.querySelector('#menu-previous').onclick=()=>{closeHeaderMenu();previousSlide();};
document.querySelector('#menu-next').onclick=()=>{closeHeaderMenu();nextSlide().catch(showError);};
document.querySelector('#menu-slideshow-toggle').onclick=()=>{closeHeaderMenu();setSlideshowPaused(!slideshowPaused);};
document.querySelector('#menu-refresh').onclick=()=>{closeHeaderMenu();render().catch(showError);};
document.querySelector('#menu-settings').onclick=()=>{closeHeaderMenu();openPreferences();};
document.querySelector('#menu-announcement').onclick=()=>{closeHeaderMenu();showAnnouncement(true);};
document.querySelector('#maintenance-unlock').onclick=async()=>{const token=maintenanceToken.value.trim();if(!token){maintenanceMessage.textContent='请输入管理密钥。';return;}maintenanceMessage.textContent='正在验证…';const response=await fetch('/api/admin/config',{headers:{'X-OpenList-Admin-Token':token},cache:'no-store'});if(!response.ok){maintenanceMessage.textContent='管理密钥无效。';return;}maintenanceAccessToken=token;maintenanceMessage.textContent='';render().catch(showError);};
lightboxDownload.onclick=()=>downloadImage().catch(showError);
document.querySelector('#preferences-save').onclick=()=>{settings.view_layout=layoutMode.value;settings.slideshow_interval=Math.max(0,Math.min(300,Number(slideshowInterval.value)||0));settings.caption_mode=captionMode.value;settings.show_tags_enabled=showTagsEnabled.checked;settings.filter_mode=filterMode.value;settings.preview_quality=previewQuality.value;settings.lightbox_quality=lightboxQuality.value;persistPreferences();location.reload();};
document.querySelector('#preferences-reset').onclick=()=>{localStorage.removeItem(PREFERENCE_KEY);location.reload();};
themeFab.onclick=()=>{if(!settings)return;settings.theme=settings.theme==='light'?'dark':'light';applyGalleryTheme(settings.theme);persistPreferences();};
document.querySelector('#preferences-close').onclick=closePreferences;
preferencesBackdrop.onclick=closePreferences;
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
  maybeLoadMoreWaterfall();
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
loadSettings().then(value=>{settings=value;taggingConfig=settings.tagging;applyGalleryTheme(settings.theme);const annVisible=settings.announcement.enabled;announcementButton.classList.toggle('hidden',!annVisible);document.querySelector('#menu-announcement').classList.toggle('hidden',!annVisible);showAnnouncement();loadTagCategories();return render();}).catch(showError);
</script>
</body>
</html>"""


def admin_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>图库管理</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{max-width:1080px;margin:auto;padding:22px;background:#10131a;color:#e7edf7;font:15px system-ui,sans-serif;transition:background .25s,color .25s}
body.theme-light{color-scheme:light;background:#eef1f6;color:#1a2333}
section{margin-bottom:18px;padding:18px;background:#171c27;border:1px solid #293040;border-radius:10px;transition:background .25s,border-color .25s}
body.theme-light section{background:#fff;border-color:#dde3ee}
h1,h2,h3{margin-top:0}
label{display:grid;gap:6px;margin:10px 0;color:inherit}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>label{flex:1;min-width:180px}
input,select,textarea,button{font:inherit}
input,select,textarea{width:100%;padding:9px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff;transition:border-color .2s,background .25s}
body.theme-light input,body.theme-light select,body.theme-light textarea{border-color:#c8d1e0;background:#f7f9fc;color:#1a2333}
input:focus,select:focus,textarea:focus{outline:none;border-color:#4b8cff}
textarea{min-height:80px;resize:vertical}
button{padding:9px 16px;border:0;border-radius:7px;background:#4b8cff;color:#fff;cursor:pointer;transition:transform .12s,background .2s,box-shadow .2s}
button:hover{background:#3a7af0;transform:translateY(-1px);box-shadow:0 4px 12px rgba(75,140,255,.35)}
button:active{transform:translateY(0) scale(.97);box-shadow:0 1px 4px rgba(75,140,255,.3)}
button.secondary{background:#39445a}
button.secondary:hover{background:#445067;box-shadow:0 4px 12px rgba(0,0,0,.2)}
.actions{margin-top:14px}
.note{color:#a9b7cd}
body.theme-light .note{color:#6b7689}
.selected{display:grid;gap:5px;margin-top:10px}
.selected-item{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:8px;border:1px solid #293040;border-radius:7px}
body.theme-light .selected-item{border-color:#dde3ee}
.check{display:flex;align-items:center;gap:8px}
.check input{width:auto}
.markdown-preview{min-height:80px;padding:12px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff;line-height:1.65}
body.theme-light .markdown-preview{border-color:#c8d1e0;background:#f7f9fc;color:#1a2333}
.markdown-preview h1,.markdown-preview h2,.markdown-preview h3,.markdown-preview h4{margin:12px 0 7px}
.markdown-preview p{margin:7px 0}
.markdown-preview code{padding:2px 5px;border-radius:4px;background:#293040}
body.theme-light .markdown-preview code{background:#e8edf5}
.hidden{display:none}
.trash-list{display:flex;flex-direction:column;gap:4px;margin-top:10px;max-height:400px;overflow-y:auto}
.trash-item{display:flex;align-items:center;gap:8px;padding:6px 10px;border:1px solid #3a455b;border-radius:6px;background:#0d1119}
.trash-item input[type=checkbox]{width:auto}
.trash-item .trash-path{flex:1;word-break:break-all;font-size:13px}
a{color:#b7d1ff}
body.theme-light a{color:#2d6cf0}
.tabs-nav{display:flex;gap:4px;flex-wrap:wrap;border-bottom:2px solid #293040;margin-bottom:20px}
body.theme-light .tabs-nav{border-color:#dde3ee}
.tab-button{padding:11px 20px;background:transparent;color:inherit;border:0;border-radius:8px 8px 0 0;cursor:pointer;font:inherit;position:relative;transition:background .2s,color .2s}
.tab-button:hover{background:rgba(75,140,255,.12);transform:none;box-shadow:none}
.tab-button.active{background:rgba(75,140,255,.18);color:#7db0ff;font-weight:600}
.tab-button.active::after{content:'';position:absolute;left:0;right:0;bottom:-2px;height:2px;background:#4b8cff;border-radius:2px}
body.theme-light .tab-button.active{color:#2d6cf0;background:rgba(45,108,240,.12)}
.tab-panel{display:none}
.tab-panel.active{display:block;animation:fadeIn .22s ease}
@keyframes fadeIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:translateY(0)}}
.tree{max-height:440px;overflow:auto;border:1px solid #293040;border-radius:7px;padding:8px}
body.theme-light .tree{border-color:#dde3ee}
.tree-node{margin:2px 0}
.tree-row{display:flex;align-items:center;gap:6px;padding:5px 6px;border-radius:5px;cursor:pointer;transition:background .15s}
.tree-row:hover{background:rgba(75,140,255,.1)}
.tree-toggle{width:18px;text-align:center;cursor:pointer;user-select:none;font-size:12px;color:#7db0ff}
.tree-toggle.tree-leaf{cursor:default}
.tree-check{width:auto;cursor:pointer}
.tree-label{flex:1;word-break:break-all}
.tree-children{margin-left:22px;display:none}
.tree-node.open>.tree-children{display:block}
.theme-toggle{position:fixed;bottom:22px;right:22px;z-index:60;width:46px;height:46px;padding:0;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:0 6px 18px rgba(0,0,0,.35);transition:transform .15s ease,box-shadow .15s ease}
.theme-toggle:hover{transform:translateY(-2px);box-shadow:0 10px 24px rgba(0,0,0,.45)}
.tabs-nav{flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:thin}
.row{flex-wrap:wrap}
@media(max-width:560px){body{padding:14px 12px}.row.actions button{flex:1 1 auto}}
</style>
</head>
<body>
<button id="theme-toggle" class="theme-toggle secondary" type="button" title="切换明暗主题">🌙</button>
<h1>图库管理</h1>
<p><a href="/gallery">返回图片浏览</a></p>
<section>
  <h2>服务器管理认证</h2>
  <label>WebUI 管理令牌<input id="token" type="password" autocomplete="current-password"></label>
  <button id="load" type="button">加载服务器配置</button>
  <p class="note hidden" id="admin-status" aria-live="polite"></p>
</section>
<section id="protected" class="hidden">
  <div class="tabs-nav">
    <button class="tab-button active" data-tab="directories" type="button">目录配置</button>
    <button class="tab-button" data-tab="display" type="button">显示与主题</button>
    <button class="tab-button" data-tab="announcement" type="button">网站公告</button>
    <button class="tab-button" data-tab="maintenance" type="button">维护模式</button>
    <button class="tab-button" data-tab="tags" type="button">标签管理</button>
    <button class="tab-button" data-tab="tools" type="button">索引与备份</button>
  </div>
  <p class="note">以下选项影响所有设备。并发浏览不会互相修改配置；若多名管理员同时保存，以最后一次保存为准。</p>

  <div id="tab-directories" class="tab-panel active">
    <h2>浏览 OpenList 目录</h2>
    <div class="row actions">
      <label style="flex:2">当前路径<input id="path" value="/"></label>
      <button id="browse" type="button" style="align-self:flex-end">加载目录树</button>
    </div>
    <p class="note">目录树实时读取自 OpenList，总是最新状态，无需刷新缓存。勾选目录前的复选框即可加入已选列表；点击目录名可展开/折叠子目录。</p>
    <div id="directories" class="tree"><p class="note">点击“加载目录树”开始浏览。</p></div>
    <h3>已选目录</h3>
    <div id="selected" class="selected"></div>
  </div>

  <div id="tab-display" class="tab-panel">
    <h2>显示设置</h2>
    <label>新访客的图片文字默认值<select id="default-caption"><option value="path">完整路径</option><option value="name">仅图片名称</option><option value="hidden">不展示</option></select></label>
    <h3>目录展示</h3>
    <label class="check"><input id="directory-display-enabled" type="checkbox">允许访客在“完整路径”模式下看到目录</label>
    <label>隐藏前 N 层目录（0 表示完整展示）<input id="directory-display-depth" type="number" min="0" max="64" step="1"></label>
    <p class="note">示例：路径 1/2/3/4，层级 1 显示为 2/3/4；层级 0 显示完整路径。</p>
    <h3>主题</h3>
    <label>管理页与浏览页默认主题<select id="theme"><option value="dark">暗色</option><option value="light">浅色</option></select></label>
    <p class="note">访客仍可在浏览页临时切换明暗；此选项仅决定默认主题。</p>
  </div>

  <div id="tab-announcement" class="tab-panel">
    <h2>网站公告</h2>
    <label class="check"><input id="announcement-enabled" type="checkbox">启用公告弹窗</label>
    <label>公告标题<input id="announcement-title" maxlength="120"></label>
    <label>公告内容（Markdown）<textarea id="announcement-content" maxlength="4000" placeholder="# 标题&#10;支持 **加粗**、*斜体*、`代码`、链接和代码块"></textarea></label>
    <button id="announcement-preview-button" class="secondary" type="button">预览公告</button>
    <div id="announcement-preview" class="markdown-preview"></div>
    <label>强制阅读秒数（0–3600）<input id="announcement-required-seconds" type="number" min="0" max="3600" step="1"></label>
    <p class="note">启用后，访客必须等待指定秒数，才能关闭或设置当前公告版本不再显示。修改标题、内容、开关或秒数都会生成新公告版本。</p>
  </div>

  <div id="tab-maintenance" class="tab-panel">
    <h2>维护模式</h2>
    <label class="check"><input id="maintenance-enabled" type="checkbox">启用维护模式</label>
    <p class="note">开启后主界面仅显示“维护中”；输入管理密钥并验证成功后，当前浏览会临时解锁图片查看和下载。</p>
  </div>

  <div id="tab-tags" class="tab-panel">
    <h2>标签功能</h2>
    <label class="check"><input id="tagging-enabled" type="checkbox">启用标签功能</label>
    <label>投票范围<select id="tagging-scope"><option value="disabled">禁用</option><option value="anonymous">匿名（按 IP+UA）</option><option value="token">仅管理员令牌持有者</option></select></label>
    <p class="note">匿名模式下，每个访客的投票记录基于 IP 和浏览器指纹去重；令牌模式下，只有持有管理令牌的用户可投票。</p>
    <h3>标签分类</h3>
    <p class="note">每行一个分类名称（最多 32 个），如：男生、女生、AI、风景、动漫。访客可点击为图片打上分类标签。</p>
    <textarea id="tagging-categories" rows="5" placeholder="男生&#10;女生&#10;AI&#10;风景&#10;动漫" style="width:100%;font:inherit;padding:9px;border:1px solid #3a455b;border-radius:7px;background:#0d1119;color:#fff"></textarea>
    <h3>筛选功能</h3>
    <label class="check"><input id="filter-enabled" type="checkbox">允许访客使用标签筛选功能</label>
    <h3>日志记录</h3>
    <p class="note">设置运行时日志级别。日志输出到 systemd journal（journalctl -u openlist-image-api）。</p>
    <label>日志级别<select id="log-level"><option value="DEBUG">DEBUG（详细调试）</option><option value="INFO">INFO（常规信息，默认）</option><option value="WARNING">WARNING（仅警告）</option><option value="ERROR">ERROR（仅错误）</option></select></label>
    <div class="row actions">
      <button id="log-view" class="secondary" type="button">查看最近日志</button>
    </div>
    <pre id="log-view-result" class="markdown-preview" style="max-height:300px;overflow:auto;font-size:12px;white-space:pre-wrap;word-break:break-all"></pre>
    <h3>标签数据管理</h3>
    <div class="row actions">
      <button id="tagging-stats" class="secondary" type="button">查看标签统计</button>
      <button id="tagging-reset-path" class="secondary" type="button">清除指定图片标签</button>
      <button id="tagging-reset-all" class="secondary" type="button" style="border-color:#ff6b6b;color:#ff6b6b">清除全部标签数据</button>
    </div>
    <div id="tagging-stats-result" class="markdown-preview"></div>
    <h3>🗑️ 垃圾桶管理</h3>
    <p class="note">访客在图片上点击 🗑️ 按钮可将图片标记为垃圾图片。以下列出所有被标记的图片，可选择性删除或全部删除。<strong>删除操作会从 OpenList 永久删除原始图片文件，不可撤销！</strong></p>
    <div class="row actions">
      <button id="trash-load" class="secondary" type="button">刷新垃圾列表</button>
      <button id="trash-delete-selected" class="secondary" type="button" style="border-color:#ff6b6b;color:#ff6b6b">删除选中图片</button>
      <button id="trash-delete-all" class="secondary" type="button" style="border-color:#ff6b6b;color:#ff6b6b">删除全部垃圾图片</button>
    </div>
    <div id="trash-list" class="trash-list"><p class="note">点击"刷新垃圾列表"加载。</p></div>
    <div id="trash-result" class="markdown-preview"></div>
  </div>

  <div id="tab-tools" class="tab-panel">
    <h2>索引与备份</h2>
    <div class="row actions"><button id="save-server" type="button">保存服务器配置</button><button id="rebuild" type="button">后台重建图片索引</button><button id="backup" type="button">下载配置备份</button></div>
    <div class="row actions"><label>上传备份配置（ZIP）<input id="backup-file" type="file" accept=".zip,application/zip"></label><button id="restore-backup" class="secondary" type="button">上传并恢复备份</button></div>
    <p class="note">恢复仅覆盖可在本页面编辑的配置，不恢复管理密钥、OpenList 令牌、端口等系统设置。</p>
  </div>
  <div class="actions" style="margin-top:20px;border-top:1px solid #293040;padding-top:16px"><button id="save-server-bottom" type="button">保存服务器配置</button></div>
</section>
<script>
let config=null;
let rebuildTimer=null;
const adminStatus=document.querySelector('#admin-status');
function setAdminStatus(text){adminStatus.textContent=text;adminStatus.classList.toggle('hidden',!text);}
function auth(){return {'Content-Type':'application/json','X-OpenList-Admin-Token':document.querySelector('#token').value};}
function showSelected(){const root=document.querySelector('#selected');root.replaceChildren(...config.directories.map(path=>{const item=document.createElement('div');item.className='selected-item';const text=document.createElement('span');text.textContent=path;const remove=document.createElement('button');remove.className='secondary';remove.type='button';remove.textContent='移除';remove.onclick=()=>{config.directories=config.directories.filter(value=>value!==path);showSelected();};item.append(text,remove);return item;}));}
function escapeHtml(value){return value.replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));}
function renderMarkdown(value){return escapeHtml(value).replace(/&lt;font\s+color=(?:&quot;|&#39;)?(#[0-9a-f]{3,8}|[a-z]+)(?:&quot;|&#39;)?\s*&gt;/gi,'<span style="color:$1">').replace(/&lt;\/font&gt;/gi,'</span>').replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>').replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>').replace(/\n\n/g,'</p><p>').replace(/\n/g,'<br>');}
function previewAnnouncement(){document.querySelector('#announcement-preview').innerHTML='<p>'+renderMarkdown(document.querySelector('#announcement-content').value)+'</p>';}
function showAdmin(){document.querySelector('#default-caption').value=config.caption_mode;document.querySelector('#directory-display-enabled').checked=config.directory_display_enabled;document.querySelector('#directory-display-depth').value=config.directory_display_depth;document.querySelector('#theme').value=config.theme||'dark';document.querySelector('#announcement-enabled').checked=config.announcement_enabled;document.querySelector('#announcement-title').value=config.announcement_title;document.querySelector('#announcement-content').value=config.announcement_content;document.querySelector('#announcement-required-seconds').value=config.announcement_required_seconds;document.querySelector('#maintenance-enabled').checked=config.maintenance_enabled;document.querySelector('#tagging-enabled').checked=config.tagging_enabled||false;document.querySelector('#tagging-scope').value=config.tagging_scope||'disabled';document.querySelector('#tagging-categories').value=(config.tagging_categories||[]).join('\n');document.querySelector('#filter-enabled').checked=config.filter_enabled!==false;document.querySelector('#log-level').value=config.log_level||'INFO';document.querySelector('#protected').classList.remove('hidden');showSelected();previewAnnouncement();}
async function errorText(response,fallback){try{const data=await response.json();return data.error||fallback;}catch(error){return fallback;}}
async function load(){const response=await fetch('/api/admin/config',{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'令牌无效或服务不可用'));config=await response.json();showAdmin();setAdminStatus('服务器配置已加载');}
function addDirectory(path){if(!config.directories.includes(path)){config.directories.push(path);showSelected();setAdminStatus('已添加目录：'+path+'，请保存服务器配置');}}
function removeDirectory(path){config.directories=config.directories.filter(value=>value!==path);showSelected();}
function buildTreeNode(name,path,isDir,hasChildren){
  const node=document.createElement('div');
  node.className='tree-node';
  const row=document.createElement('div');
  row.className='tree-row';
  if(isDir){
    const toggle=document.createElement('span');
    toggle.className='tree-toggle';
    if(hasChildren===false){toggle.textContent='';toggle.classList.add('tree-leaf');}
    else{toggle.textContent='▶';}
    const check=document.createElement('input');
    check.type='checkbox';
    check.className='tree-check';
    check.checked=config.directories.includes(path);
    check.onchange=()=>{if(check.checked)addDirectory(path);else removeDirectory(path);};
    const label=document.createElement('span');
    label.className='tree-label';
    label.textContent=name;
    label.onclick=()=>{
      if(hasChildren===false)return;
      node.classList.toggle('open');
      toggle.textContent=node.classList.contains('open')?'▼':'▶';
      if(node.classList.contains('open')&&!node.querySelector('.tree-children').children.length){
        loadTreeChildren(path,node).catch(report);
      }
    };
    row.append(toggle,check,label);
  }else{
    const spacer=document.createElement('span');
    spacer.className='tree-toggle';
    spacer.textContent='•';
    const label=document.createElement('span');
    label.className='tree-label';
    label.textContent=name;
    row.append(spacer,label);
  }
  node.append(row);
  const children=document.createElement('div');
  children.className='tree-children';
  node.append(children);
  return node;
}
async function loadTreeChildren(path,parent){
  const container=parent.querySelector('.tree-children');
  container.innerHTML='<p class="note" style="margin-left:24px">正在读取目录…</p>';
  const response=await fetch('/api/admin/directories?path='+encodeURIComponent(path),{headers:auth(),cache:'no-store'});
  if(!response.ok)throw new Error(await errorText(response,'无法读取目录'));
  const data=await response.json();
  container.replaceChildren();
  if(!data.directories.length){container.innerHTML='<p class="note" style="margin-left:24px">无子目录</p>';const toggle=parent.querySelector('.tree-toggle');if(toggle){toggle.textContent='';toggle.classList.add('tree-leaf');}return;}
  data.directories.forEach(item=>{
    const child=buildTreeNode(item.name,item.path,true,item.has_children);
    container.append(child);
  });
}
async function browse(){
  if(!config)throw new Error('请先加载服务器配置');
  const path=document.querySelector('#path').value||'/';
  const response=await fetch('/api/admin/directories?path='+encodeURIComponent(path),{headers:auth(),cache:'no-store'});
  if(!response.ok)throw new Error(await errorText(response,'无法读取目录缓存'));
  const data=await response.json();
  const root=document.querySelector('#directories');
  root.replaceChildren();
  const node=buildTreeNode(path||'/',path||'/',true,data.directories.length>0);
  node.classList.add('open');
  const toggle=node.querySelector('.tree-toggle');
  if(toggle&&!toggle.classList.contains('tree-leaf'))toggle.textContent='▼';
  root.append(node);
  const container=node.querySelector('.tree-children');
  if(!data.directories.length){container.innerHTML='<p class="note">当前目录没有子目录。</p>';}
  else{data.directories.forEach(item=>{const child=buildTreeNode(item.name,item.path,true,item.has_children);container.append(child);});}
}
async function saveServer(){if(!config)throw new Error('请先加载服务器配置');const payload={directories:config.directories,caption_mode:document.querySelector('#default-caption').value,directory_display_enabled:document.querySelector('#directory-display-enabled').checked,directory_display_depth:Number(document.querySelector('#directory-display-depth').value),theme:document.querySelector('#theme').value,announcement_enabled:document.querySelector('#announcement-enabled').checked,announcement_title:document.querySelector('#announcement-title').value,announcement_content:document.querySelector('#announcement-content').value,announcement_required_seconds:Number(document.querySelector('#announcement-required-seconds').value),maintenance_enabled:document.querySelector('#maintenance-enabled').checked,tagging_enabled:document.querySelector('#tagging-enabled').checked,tagging_scope:document.querySelector('#tagging-scope').value,tagging_categories:document.querySelector('#tagging-categories').value.split('\n').map(s=>s.trim()).filter(Boolean),filter_enabled:document.querySelector('#filter-enabled').checked,log_level:document.querySelector('#log-level').value};const response=await fetch('/api/admin/config',{method:'PUT',headers:auth(),body:JSON.stringify(payload)});if(!response.ok)throw new Error(await errorText(response,'保存失败'));config=await response.json();showAdmin();setAdminStatus('全局服务器配置已保存；公告修改后将向访客显示新版本。');}
function formatSeconds(value){const seconds=Math.max(1,Math.round(Number(value)||0));return seconds<60?seconds+' 秒':Math.ceil(seconds/60)+' 分钟';}
async function pollRebuild(doneMessage='索引后台重建完成'){const response=await fetch('/api/status',{cache:'no-store'});if(!response.ok)return;if((await response.json()).refreshing){rebuildTimer=setTimeout(()=>pollRebuild(doneMessage).catch(report),2000);}else{setAdminStatus(doneMessage);rebuildTimer=null;}}
async function rebuild(){const statusResponse=await fetch('/api/status',{cache:'no-store'});const previous=statusResponse.ok?await statusResponse.json():{};const response=await fetch('/api/admin/rebuild',{method:'POST',headers:auth()});if(!response.ok)throw new Error(await errorText(response,'重建未启动'));const estimate=Number(previous.last_build_duration_seconds)||0;setAdminStatus('索引正在后台重建'+(estimate?'，预计约 '+formatSeconds(estimate):'，首次重建暂无预估时间'));clearTimeout(rebuildTimer);rebuildTimer=setTimeout(()=>pollRebuild().catch(report),2000);}
async function backup(){const response=await fetch('/api/admin/backup',{headers:auth()});if(!response.ok)throw new Error(await errorText(response,'备份下载失败'));const blob=await response.blob();const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='openlist-image-api-backup.zip';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);setAdminStatus('配置备份已下载（不含 token）');}
async function restoreBackup(){const file=document.querySelector('#backup-file').files[0];if(!file)throw new Error('请先选择 ZIP 备份文件');if(!window.confirm('确定恢复该备份中的可编辑配置吗？'))return;const response=await fetch('/api/admin/backup',{method:'POST',headers:{'X-OpenList-Admin-Token':document.querySelector('#token').value},body:file});if(!response.ok)throw new Error(await errorText(response,'备份恢复失败'));config=await response.json();showAdmin();setAdminStatus('备份配置已恢复，请按需保存或重建图片索引。');}
function report(error){setAdminStatus('操作失败：'+error.message);}
document.querySelectorAll('.tab-button').forEach(btn=>{btn.onclick=()=>{document.querySelectorAll('.tab-button').forEach(b=>b.classList.remove('active'));document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));btn.classList.add('active');document.getElementById('tab-'+btn.dataset.tab).classList.add('active');};});
function applyTheme(theme){document.body.classList.toggle('theme-light',theme==='light');document.body.classList.toggle('theme-dark',theme!=='light');document.querySelector('#theme-toggle').textContent=theme==='light'?'☀':'🌙';try{localStorage.setItem('openlist-admin-theme',theme);}catch(e){}}
function toggleTheme(){applyTheme(document.body.classList.contains('theme-light')?'dark':'light');}
document.querySelector('#theme-toggle').onclick=toggleTheme;
(function(){let saved='dark';try{saved=localStorage.getItem('openlist-admin-theme')||'dark';}catch(e){}applyTheme(saved);})();
document.querySelector('#load').onclick=()=>load().catch(report);
document.querySelector('#announcement-preview-button').onclick=previewAnnouncement;
document.querySelector('#browse').onclick=()=>browse().catch(report);
document.querySelector('#save-server').onclick=()=>saveServer().catch(report);
document.querySelector('#save-server-bottom').onclick=()=>saveServer().catch(report);
document.querySelector('#rebuild').onclick=()=>rebuild().catch(report);
document.querySelector('#backup').onclick=()=>backup().catch(report);
document.querySelector('#restore-backup').onclick=()=>restoreBackup().catch(report);
document.querySelector('#tagging-stats').onclick=()=>loadTagStats().catch(report);
document.querySelector('#log-view').onclick=async()=>{try{setAdminStatus('正在加载日志…');const response=await fetch('/api/admin/logs?lines=100',{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'加载日志失败'));const data=await response.json();document.querySelector('#log-view-result').textContent=data.logs||'(无日志)';setAdminStatus('日志已加载');}catch(error){report(error);}};
document.querySelector('#tagging-reset-path').onclick=()=>resetTagPath().catch(report);
document.querySelector('#tagging-reset-all').onclick=()=>resetTagAll().catch(report);
document.querySelector('#trash-load').onclick=()=>loadTrashList().catch(report);
document.querySelector('#trash-delete-selected').onclick=()=>deleteTrashSelected().catch(report);
document.querySelector('#trash-delete-all').onclick=()=>deleteTrashAll().catch(report);
async function loadTagStats(){const response=await fetch('/api/tagging/categories',{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'获取统计失败'));const data=await response.json();const result=document.querySelector('#tagging-stats-result');const cats=data.categories||{};const keys=Object.keys(cats);if(!keys.length){result.innerHTML='<p class="note">暂无标签数据。访客投票或打分类后，这里会显示统计。</p>';setAdminStatus('标签统计：暂无数据');return;}const items=keys.map(k=>'<li>'+escapeHtml(k)+'：'+cats[k]+' 张图片</li>').join('');result.innerHTML='<p>当前标签使用情况：</p><ul>'+items+'</ul>';setAdminStatus('标签统计已加载');}
async function resetTagPath(){const path=prompt('请输入要清除标签的图片路径：');if(!path)return;const response=await fetch('/api/admin/tagging/reset?path='+encodeURIComponent(path),{method:'POST',headers:auth()});if(!response.ok)throw new Error(await errorText(response,'清除失败'));setAdminStatus('已清除 '+path+' 的标签数据');}
async function resetTagAll(){if(!confirm('确定清除全部标签数据吗？此操作不可撤销！'))return;const response=await fetch('/api/admin/tagging/reset',{method:'POST',headers:auth()});if(!response.ok)throw new Error(await errorText(response,'清除失败'));setAdminStatus('全部标签数据已清除');}
async function loadTrashList(){const response=await fetch('/api/admin/tagging/trash',{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'获取垃圾列表失败'));const data=await response.json();const list=document.querySelector('#trash-list');const paths=data.paths||[];if(!paths.length){list.innerHTML='<p class="note">暂无垃圾图片标记。</p>';setAdminStatus('垃圾列表：暂无数据');return;}list.replaceChildren();paths.forEach(p=>{const item=document.createElement('div');item.className='trash-item';const check=document.createElement('input');check.type='checkbox';check.value=p;const label=document.createElement('span');label.className='trash-path';label.textContent=p;item.append(check,label);list.append(item);});setAdminStatus('垃圾列表已加载，共 '+paths.length+' 张图片');}
async function deleteTrashSelected(){const checks=document.querySelectorAll('#trash-list .trash-item input[type=checkbox]:checked');if(!checks.length){setAdminStatus('请先勾选要删除的图片');return;}const paths=Array.from(checks).map(c=>c.value);if(!confirm('确定删除选中的 '+paths.length+' 张图片吗？此操作会从 OpenList 永久删除原始图片文件，不可撤销！'))return;const response=await fetch('/api/admin/tagging/trash/delete',{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:JSON.stringify({paths:paths})});if(!response.ok)throw new Error(await errorText(response,'删除失败'));const result=await response.json();const resultEl=document.querySelector('#trash-result');resultEl.innerHTML='<p>删除完成：成功 '+result.deleted+' 张，失败 '+result.failed+' 张。</p>'+(result.errors&&result.errors.length?'<ul>'+result.errors.map(e=>'<li>'+escapeHtml(e.path)+'：'+escapeHtml(e.error)+'</li>').join('')+'</ul>':'');setAdminStatus('删除完成：成功 '+result.deleted+'，失败 '+result.failed);return loadTrashList();}
async function deleteTrashAll(){const response=await fetch('/api/admin/tagging/trash',{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'获取垃圾列表失败'));const data=await response.json();const paths=data.paths||[];if(!paths.length){setAdminStatus('暂无垃圾图片可删除');return;}if(!confirm('确定删除全部 '+paths.length+' 张垃圾图片吗？此操作会从 OpenList 永久删除原始图片文件，不可撤销！'))return;const delResponse=await fetch('/api/admin/tagging/trash/delete',{method:'POST',headers:{...auth(),'Content-Type':'application/json'},body:JSON.stringify({})});if(!delResponse.ok)throw new Error(await errorText(delResponse,'删除失败'));const result=await delResponse.json();const resultEl=document.querySelector('#trash-result');resultEl.innerHTML='<p>删除完成：成功 '+result.deleted+' 张，失败 '+result.failed+' 张。</p>'+(result.errors&&result.errors.length?'<ul>'+result.errors.map(e=>'<li>'+escapeHtml(e.path)+'：'+escapeHtml(e.error)+'</li>').join('')+'</ul>':'');setAdminStatus('删除完成：成功 '+result.deleted+'，失败 '+result.failed);return loadTrashList();}
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
                    request_start = time.time()
                    count = self._query_int(params, "count", 1)
                    tag_filter = params.get("tag", []) or params.get("tags", [])
                    filter_mode = params.get("filter_mode", ["union"])[0]
                    images = application.choose_images(
                        count, params.get("folder", [None])[0], parse_size(params.get("min_size", [None])[0]), parse_size(params.get("max_size", [None])[0]), tags=tag_filter or None, filter_mode=filter_mode
                    )
                    if not images:
                        logging.debug("images/random: count=%d tags=%s mode=%s -> 0 images in %.3fs", count, tag_filter, filter_mode, time.time() - request_start)
                        return self._send_json(HTTPStatus.OK, {"images": []})
                    include_tags = application.config["tagging_enabled"] and application.config["tagging_scope"] != "disabled"
                    result = application.resolve_images_lazy(images, include_tags=include_tags)
                    cached = sum(1 for r in result if not r.get("needs_url"))
                    logging.debug("images/random: count=%d tags=%s mode=%s -> %d images (%d cached) in %.3fs", count, tag_filter, filter_mode, len(result), cached, time.time() - request_start)
                    return self._send_json(HTTPStatus.OK, {"images": result})
                if parsed.path == "/api/download-url":
                    if self._maintenance_access_required():
                        return
                    raw_path = params.get("path", [""])[0]
                    refresh = params.get("fresh", ["0"])[0].lower() in {"1", "true", "yes"}
                    image = application.indexed_image(raw_path)
                    resolved = application.resolve_images([image], refresh=refresh)[0]
                    return self._send_json(HTTPStatus.OK, {"url": resolved["url"], "thumbnail": resolved.get("thumbnail", "")})
                if parsed.path == "/download":
                    if self._maintenance_access_required():
                        return
                    raw_path = params.get("path", [""])[0]
                    image = application.indexed_image(raw_path)
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
                if parsed.path == "/api/tagging/stats":
                    paths = [p for p in params.get("paths", [""])[0].split(",") if p]
                    if not paths:
                        return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "paths parameter required"})
                    if len(paths) > 50:
                        return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "too many paths"})
                    return self._send_json(HTTPStatus.OK, {"stats": application.tags.stats(paths)})
                if parsed.path == "/api/tagging/categories":
                    return self._send_json(HTTPStatus.OK, {"categories": application.tags.all_categories()})
                if parsed.path == "/api/admin/tagging/trash":
                    if not self._admin_required():
                        return
                    return self._send_json(HTTPStatus.OK, {"paths": application.trash_paths(), "trash_tag": application.TRASH_TAG})
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
                if parsed.path == "/api/admin/logs":
                    if not self._admin_required():
                        return
                    lines = int(params.get("lines", ["100"])[0])
                    lines = max(1, min(lines, 500))
                    try:
                        result = subprocess.run(
                            ["journalctl", "-u", "openlist-image-api", "--no-pager", "-n", str(lines), "--output=short-iso"],
                            capture_output=True, text=True, timeout=10,
                        )
                        return self._send_json(HTTPStatus.OK, {"logs": result.stdout})
                    except Exception as error:
                        return self._send_json(HTTPStatus.OK, {"logs": "", "error": str(error)})
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
            if path == "/api/download-url":
                return self._handle_download_urls()
            if path == "/api/tagging/vote":
                return self._handle_tag_vote()
            if not self._admin_required():
                return
            try:
                if path == "/api/admin/rebuild":
                    if application.start_refresh():
                        return self._send_json(HTTPStatus.ACCEPTED, {"status": "rebuild started"})
                    return self._send_json(HTTPStatus.CONFLICT, {"error": "a rebuild is already running"})
                if path == "/api/admin/backup":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_REQUEST_BODY:
                        raise ValueError("invalid request body size")
                    return self._send_json(HTTPStatus.OK, application.restore_config_backup(self.rfile.read(length)))
                if path == "/api/admin/tagging/reset":
                    target = parse_qs(urlparse(self.path).query).get("path", [None])[0]
                    if target:
                        application.tags.reset_path(normalize_directory(target))
                    else:
                        application.tags.reset_all()
                    return self._send_json(HTTPStatus.OK, {"status": "tags reset"})
                if path == "/api/admin/tagging/trash/delete":
                    length = int(self.headers.get("Content-Length", "0"))
                    selected: list[str] | None = None
                    if 0 < length <= MAX_REQUEST_BODY:
                        payload = json.loads(self.rfile.read(length))
                        if isinstance(payload, dict) and isinstance(payload.get("paths"), list):
                            selected = [normalize_directory(p) for p in payload["paths"] if isinstance(p, str) and p]
                    result = application.delete_trash_images(selected)
                    return self._send_json(HTTPStatus.OK, result)
                return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except (ValueError, RuntimeError, zipfile.BadZipFile) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception:
                logging.exception("Unhandled POST error")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

        def _handle_download_urls(self) -> None:
            if self._maintenance_access_required():
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BODY:
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body size"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request body must be an object"})
                raw_paths = payload.get("paths")
                if not isinstance(raw_paths, list):
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "paths must be an array"})
                paths = [path for path in raw_paths if isinstance(path, str) and path]
                if not paths:
                    return self._send_json(HTTPStatus.OK, {"images": []})
                refresh = payload.get("fresh")
                if isinstance(refresh, str):
                    refresh = refresh.lower() in {"1", "true", "yes"}
                else:
                    refresh = bool(refresh)
                return self._send_json(HTTPStatus.OK, {"images": application.resolve_download_urls(paths, refresh=refresh)})
            except (ValueError, RuntimeError, json.JSONDecodeError) as error:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception:
                logging.exception("Unhandled download-url POST error")
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

        def _handle_tag_vote(self) -> None:
            try:
                if not application.config["tagging_enabled"] or application.config["tagging_scope"] == "disabled":
                    return self._send_json(HTTPStatus.FORBIDDEN, {"error": "tagging is disabled"})
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BODY:
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request body size"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "request body must be an object"})
                image_path = payload.get("path")
                vote_type = payload.get("type")
                value = payload.get("value")
                if not isinstance(image_path, str) or not image_path:
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "path is required"})
                if vote_type not in {"like", "dislike", "category"}:
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid vote type"})
                if not isinstance(value, bool):
                    return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "value must be a boolean"})
                admin_token = admin_token_from_headers(self.headers)
                scope = application.config["tagging_scope"]
                if scope == "token":
                    try:
                        authorized = bool(admin_token) and application.is_admin(admin_token)
                    except RuntimeError:
                        authorized = False
                    if not authorized:
                        return self._send_json(HTTPStatus.FORBIDDEN, {"error": "valid admin token required for token-scope tagging"})
                normalized = normalize_directory(image_path)
                application.indexed_image(normalized)
                voter = application.voter_id(self.client_address[0], self.headers.get("User-Agent", ""), admin_token)
                if not voter:
                    return self._send_json(HTTPStatus.FORBIDDEN, {"error": "unable to identify voter"})
                if vote_type == "category":
                    category = str(payload.get("category", "")).strip()
                    if not category:
                        return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "category is required"})
                    categories = application.config["tagging_categories"]
                    is_trash = category == application.TRASH_TAG
                    if not is_trash and category not in categories:
                        return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "category not allowed"})
                    if len(category) > 32:
                        return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "category name too long"})
                    result = application.tags.set_category(normalized, category, value)
                else:
                    result = application.tags.vote(normalized, voter, vote_type, value)
                return self._send_json(HTTPStatus.OK, result)
            except (ValueError, RuntimeError, json.JSONDecodeError) as error:
                return self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            except Exception:
                logging.exception("Unhandled tag vote error")
                return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal server error"})

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
    valid_paths = {img["path"] for img in index.get("images", []) if isinstance(img, dict) and "path" in img}
    migration_stats = {}
    if valid_paths:
        migration_stats = application.tags.migrate_paths(valid_paths)
    print(json.dumps({"image_count": index["image_count"], "directory_count": index["directory_count"], "errors": index["errors"], "tag_migration": migration_stats}, ensure_ascii=False))


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
