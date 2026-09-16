#!/usr/bin/env python3
"""Secure, dependency-free random-image API for OpenList."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import secrets
import socket
import threading
import time
import zipfile
from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
    "contact_enabled": False,
    "contact_label": "联系",
    "contact_personal_label": "个人",
    "contact_personal_url": "",
    "contact_personal_image": "",
    "contact_group_label": "群组",
    "contact_group_url": "",
    "contact_group_image": "",
    "maintenance_enabled": False,
    "theme": "dark",
    "grid_gap": 12,
    "grid_scale": 150,
    "url_cache_size": 0,
    "url_cache_ttl_seconds": 7200,
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
URL_RESOLVE_WORKERS = 12
URL_RESOLVE_WAIT_SECONDS = 4
INDEX_LIST_TIMEOUT_SECONDS = 10
INDEX_LIST_WORKERS = 4
INDEX_CHECKPOINT_INTERVAL = 32
SHARED_CHAIN_LENGTH = 4000
SHARED_CHAIN_TTL_SECONDS = 7200
BAIDU_PHOTO_DRIVER = "BaiduPhoto"
DIGIT_FOLDER_RE = re.compile(r"^\d+$")
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


def normalize_http_url(value: Any, key: str, limit: int = 300) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if not value:
        return ""
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{key} must be an http or https URL")
    if len(value) > limit:
        raise ValueError(f"{key} is too long")
    return value


def normalize_contact_text(value: Any, key: str, default: str = "", limit: int = 20) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip() or default
    if len(value) > limit:
        raise ValueError(f"{key} is too long")
    return value


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
    if not isinstance(config["contact_enabled"], bool):
        raise ValueError("contact_enabled must be a boolean")
    config["contact_label"] = normalize_contact_text(config["contact_label"], "contact_label", "联系")
    config["contact_personal_label"] = normalize_contact_text(config["contact_personal_label"], "contact_personal_label", "个人")
    config["contact_personal_url"] = normalize_http_url(config["contact_personal_url"], "contact_personal_url")
    config["contact_personal_image"] = normalize_http_url(config["contact_personal_image"], "contact_personal_image")
    config["contact_group_label"] = normalize_contact_text(config["contact_group_label"], "contact_group_label", "群组")
    config["contact_group_url"] = normalize_http_url(config["contact_group_url"], "contact_group_url")
    config["contact_group_image"] = normalize_http_url(config["contact_group_image"], "contact_group_image")
    has_personal = bool(config["contact_personal_url"] or config["contact_personal_image"])
    has_group = bool(config["contact_group_url"] or config["contact_group_image"])
    if config["contact_enabled"] and not has_personal and not has_group:
        raise ValueError("contact requires a personal or group link, image, or QQ number")
    if not isinstance(config["maintenance_enabled"], bool):
        raise ValueError("maintenance_enabled must be a boolean")
    if config["theme"] not in {"light", "dark"}:
        raise ValueError("theme must be light or dark")
    if not isinstance(config["grid_gap"], int) or not 0 <= config["grid_gap"] <= 48:
        raise ValueError("grid_gap must be between 0 and 48")
    if not isinstance(config["grid_scale"], int) or not 75 <= config["grid_scale"] <= 200:
        raise ValueError("grid_scale must be between 75 and 200")
    if not isinstance(config["url_cache_size"], int) or not 0 <= config["url_cache_size"] <= 8000:
        raise ValueError("invalid url_cache_size")
    if not isinstance(config["url_cache_ttl_seconds"], int) or not 0 <= config["url_cache_ttl_seconds"] <= 7200:
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
        if not isinstance(config[key], str) or not (config[key].startswith("/") or (len(config[key]) >= 2 and config[key][1] == ":")):
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

    def _request_json(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 15,
        retries: int = 1,
        retry_throttled_only: bool = False,
    ) -> dict[str, Any]:
        token = read_secret_cached(self.token_path, "OpenList API token")
        request_start = time.time()
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Authorization": token}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            f"{self.base_url}{endpoint}",
            data=body,
            headers=headers,
            method=method,
        )
        last_error: Exception | None = None
        attempts = max(1, retries + 1)
        for attempt in range(attempts):
            try:
                with urlopen(request, timeout=timeout) as response:
                    result = json.load(response)
                if result.get("code") != 200:
                    raise RuntimeError(result.get("message") or "OpenList rejected request")
                data = result.get("data") or {}
                if not isinstance(data, dict):
                    raise RuntimeError("OpenList returned invalid data")
                logging.debug("OpenList %s %s: %.3fs (attempt %d)", method, endpoint, time.time() - request_start, attempt + 1)
                return data
            except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as error:
                last_error = error
                logging.debug("OpenList %s %s failed attempt %d: %s", method, endpoint, attempt + 1, error)
                throttled = (
                    isinstance(error, HTTPError) and error.code == HTTPStatus.TOO_MANY_REQUESTS
                ) or "throttl" in str(error).lower()
                if (
                    isinstance(error, TimeoutError)
                    or attempt >= attempts - 1
                    or (retry_throttled_only and not throttled)
                ):
                    break
                time.sleep(1.0 if throttled else 0.5)
        raise RuntimeError(f"OpenList request failed: {last_error}")

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        timeout: float = 15,
        retries: int = 1,
        retry_throttled_only: bool = False,
    ) -> dict[str, Any]:
        return self._request_json(
            endpoint,
            method="POST",
            payload=payload,
            timeout=timeout,
            retries=retries,
            retry_throttled_only=retry_throttled_only,
        )

    def _get(
        self,
        endpoint: str,
        timeout: float = 15,
        retries: int = 1,
        retry_throttled_only: bool = False,
    ) -> dict[str, Any]:
        return self._request_json(
            endpoint,
            method="GET",
            timeout=timeout,
            retries=retries,
            retry_throttled_only=retry_throttled_only,
        )

    def list_directory(self, path: str, index_scan: bool = False) -> list[dict[str, Any]]:
        page = 1
        entries: list[dict[str, Any]] = []
        timeout = INDEX_LIST_TIMEOUT_SECONDS if index_scan else 15
        while True:
            data = self._post(
                "/api/fs/list",
                {"path": path, "password": "", "page": page, "per_page": 1000, "refresh": False},
                timeout=timeout,
                retries=1,
                retry_throttled_only=index_scan,
            )
            content = data.get("content") or []
            if not isinstance(content, list):
                raise RuntimeError("OpenList returned invalid directory content")
            entries.extend(item for item in content if isinstance(item, dict))
            try:
                total = int(data.get("total") or len(entries))
            except (TypeError, ValueError) as error:
                raise RuntimeError("OpenList returned invalid directory total") from error
            if total <= len(entries) or not content:
                return entries
            page += 1

    def path_provider(self, path: str) -> str:
        data = self._post(
            "/api/fs/get",
            {"path": path, "password": "", "refresh": False},
            timeout=8,
            retries=1,
            retry_throttled_only=True,
        )
        return str(data.get("provider") or "").strip()

    def storage_drivers(self) -> dict[str, str]:
        """Return mount_path -> driver. Never exposes storage addition secrets."""
        try:
            data = self._get("/api/admin/storage/list", timeout=10, retries=1)
        except Exception as error:
            logging.debug("OpenList storage list unavailable: %s", error)
            return {}
        content = data.get("content") or data.get("storages") or []
        if not isinstance(content, list):
            return {}
        drivers: dict[str, str] = {}
        for item in content:
            if not isinstance(item, dict):
                continue
            mount = str(item.get("mount_path") or "").strip()
            driver = str(item.get("driver") or "").strip()
            if not mount or not driver:
                continue
            try:
                mount = normalize_directory(mount)
            except ValueError:
                continue
            drivers[mount] = driver
        return drivers

    def baidu_photo_mounts(self) -> set[str]:
        return {path for path, driver in self.storage_drivers().items() if driver == BAIDU_PHOTO_DRIVER}

    def resolve_file(self, path: str) -> tuple[str, str]:
        data = self._post(
            "/api/fs/get",
            {"path": path, "password": "", "refresh": False},
            timeout=8,
            retries=1,
            retry_throttled_only=True,
        )
        url = str(data.get("raw_url") or data.get("url") or "").strip()
        if not url:
            raise RuntimeError("OpenList did not return a file URL")
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("OpenList returned an invalid file URL")
        return url, self._safe_thumb(data.get("thumb"))

    def resolve_preview(self, path: str) -> tuple[str, str]:
        data = self._post(
            "/api/fs/get",
            {"path": path, "password": "", "refresh": False},
            timeout=8,
            retries=1,
            retry_throttled_only=True,
        )
        thumb = self._safe_thumb(data.get("thumb"))
        if not thumb:
            raise RuntimeError("OpenList did not return a thumbnail")
        return "", thumb

    @staticmethod
    def _safe_thumb(value: Any) -> str:
        thumb = str(value or "").strip()
        if not thumb:
            return ""
        parsed = urlparse(thumb)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return thumb

    def remove_file(self, path: str) -> None:
        parts = path.rsplit("/", 1)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise RuntimeError(f"invalid file path for removal: {path}")
        dir_path, name = parts[0], parts[1]
        self._post("/api/fs/remove", {"dir": dir_path, "names": [name]})


def baidu_photo_scan_mode(path: str, baidu_mounts: set[str]) -> str:
    """Return mount | inside | normal for BaiduPhoto indexing/browse policy."""
    normalized = normalize_directory(path)
    for mount in baidu_mounts:
        if normalized == mount:
            return "mount"
        if normalized.startswith(mount.rstrip("/") + "/"):
            return "inside"
    return "normal"


def discover_baidu_photo_mounts(client: OpenListClient, candidate_paths: list[str] | None = None) -> set[str]:
    mounts = set(client.baidu_photo_mounts())
    for raw in candidate_paths or []:
        try:
            path = normalize_directory(raw)
        except ValueError:
            continue
        if path in mounts:
            continue
        try:
            if client.path_provider(path) == BAIDU_PHOTO_DRIVER:
                mounts.add(path)
        except Exception:
            continue
    return mounts


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


TRASH_TAG = "垃圾桶"
LEGACY_TRASH_TAG = "🗑️ 垃圾桶"


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
            changed = self._normalize_trash_tag(data)
            self._cache = data
            if changed:
                self.save(data)
            return self._cache

    def _normalize_trash_tag(self, data: dict[str, Any]) -> bool:
        changed = False
        for entry in data.get("tags", {}).values():
            if not isinstance(entry, dict):
                continue
            categories = entry.get("categories")
            if not isinstance(categories, list) or LEGACY_TRASH_TAG not in categories:
                continue
            normalized: list[str] = []
            seen: set[str] = set()
            for item in categories:
                name = TRASH_TAG if item == LEGACY_TRASH_TAG else item
                if name in seen:
                    continue
                seen.add(name)
                normalized.append(name)
            entry["categories"] = normalized
            changed = True
        return changed

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


def _index_config_fingerprint(config: dict[str, Any]) -> str:
    payload = {
        "directories": config["directories"],
        "extensions": sorted(config["extensions"]),
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def build_index(
    config: dict[str, Any],
    repository: IndexRepository,
    progress: Any = None,
) -> dict[str, Any]:
    started_at = time.time()
    client = OpenListClient(config)
    extensions = set(config["extensions"])
    state_dir = Path(config["state_dir"])
    checkpoint_path = state_dir / "index.checkpoint.json"
    fingerprint = _index_config_fingerprint(config)
    queue: deque[str] = deque()
    visited: set[str] = set()
    images: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    retry_pending: deque[str] = deque()
    resumed = False
    baidu_mounts = discover_baidu_photo_mounts(client, list(config["directories"]))

    try:
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if isinstance(checkpoint, dict) and checkpoint.get("fingerprint") == fingerprint:
            raw_queue = checkpoint.get("queue", [])
            raw_visited = checkpoint.get("visited", [])
            raw_images = checkpoint.get("images", [])
            raw_retry = checkpoint.get("retry_pending", [])
            if all(isinstance(item, str) for item in raw_queue + raw_visited + raw_retry) and isinstance(raw_images, list):
                queue.extend(raw_queue)
                visited.update(raw_visited)
                images.extend(item for item in raw_images if isinstance(item, dict))
                retry_pending.extend(raw_retry)
                started_at = float(checkpoint.get("started_at") or started_at)
                resumed = True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass

    if not resumed:
        for directory in config["directories"]:
            if directory not in visited:
                visited.add(directory)
                queue.append(directory)

    completed = 0
    retry_round = False
    failures: list[tuple[str, str]] = []
    state_lock = threading.Lock()

    def report() -> None:
        if progress is None:
            return
        with state_lock:
            progress({
                "completed": completed,
                "queued": len(queue),
                "active": len(active),
                "failed": len(failures) + len(retry_pending),
                "directory_count": len(visited),
                "image_count": len(images),
                "elapsed_seconds": round(time.time() - started_at, 2),
                "retry_round": retry_round,
            })

    def save_checkpoint() -> None:
        active_paths = [path for path in active.values()]
        payload = {
            "version": 1,
            "fingerprint": fingerprint,
            "started_at": started_at,
            "queue": list(queue) + active_paths,
            "visited": sorted(visited),
            "images": images,
            "retry_pending": list(retry_pending) + [path for path, _error in failures],
        }
        try:
            atomic_write_json(checkpoint_path, payload)
        except OSError:
            logging.warning("Unable to save index checkpoint")

    def handle_entries(current: str, entries: list[dict[str, Any]]) -> None:
        mode = baidu_photo_scan_mode(current, baidu_mounts)
        for entry in entries:
            name = str(entry.get("name") or "")
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                continue
            path = join_virtual_path(current, name)
            if entry.get("is_dir"):
                if mode == "mount":
                    # BaiduPhoto mount root: only pure-digit author albums.
                    if not DIGIT_FOLDER_RE.fullmatch(name):
                        continue
                    if path not in visited:
                        visited.add(path)
                        queue.append(path)
                elif mode == "inside":
                    # Author album content is flat; do not descend further.
                    continue
                else:
                    if path not in visited:
                        visited.add(path)
                        queue.append(path)
            elif Path(name).suffix.lower() in extensions:
                try:
                    size = max(0, int(entry.get("size") or 0))
                except (TypeError, ValueError):
                    size = 0
                images.append({"path": path, "size": size})

    active: dict[Any, str] = {}
    save_checkpoint()
    with ThreadPoolExecutor(max_workers=INDEX_LIST_WORKERS, thread_name_prefix="openlist-index") as executor:
        while queue or active or retry_pending or failures:
            if not active and not queue and failures and not retry_round:
                retry_pending.extend(path for path, _error in failures)
                failures.clear()
                retry_round = True
            if not active and not queue and retry_pending:
                retry_round = True
                queue.extend(retry_pending)
                retry_pending.clear()
                continue
            limit = 1 if retry_round else INDEX_LIST_WORKERS
            while queue and len(active) < limit:
                current = queue.popleft()
                future = executor.submit(client.list_directory, current, True)
                active[future] = current
            if not active:
                if retry_pending:
                    queue.extend(retry_pending)
                    retry_pending.clear()
                    continue
                break
            done, _pending = wait(tuple(active), return_when=FIRST_COMPLETED)
            for future in done:
                current = active.pop(future)
                completed += 1
                try:
                    handle_entries(current, future.result())
                except Exception as error:
                    message = str(error)
                    if not retry_round:
                        logging.warning("Retrying directory %s: %s", current, message)
                        failures.append((current, message))
                    else:
                        logging.warning("Skipping directory %s: %s", current, message)
                        errors.append({"directory": current, "error": message})
            if completed % INDEX_CHECKPOINT_INTERVAL == 0 or not active:
                save_checkpoint()
            report()

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
    try:
        checkpoint_path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logging.warning("Unable to remove index checkpoint")
    if progress is not None:
        progress({
            "completed": completed,
            "queued": 0,
            "active": 0,
            "failed": len(errors),
            "directory_count": len(visited),
            "image_count": len(images),
            "elapsed_seconds": round(time.time() - started_at, 2),
            "retry_round": retry_round,
            "complete": True,
        })
    return index


class _InflightResolve:
    __slots__ = ("event", "result", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: tuple[str, str] | None = None
        self.error: BaseException | None = None


class UrlCache:
    def __init__(self, max_size: int, ttl_seconds: int, persist_path: Path | None = None):
        self.max_size = max_size
        self.ttl_seconds = ttl_seconds
        self.persist_path = persist_path
        self._entries: OrderedDict[str, tuple[float, str, str]] = OrderedDict()
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str, str], _InflightResolve] = {}
        self._save_timer: threading.Timer | None = None
        self.hits = 0
        self.misses = 0
        self._load_persisted()

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

    def cached_pair(self, path: str) -> tuple[str, str] | None:
        return self._cached(path)

    def _load_persisted(self) -> None:
        if not self.persist_path or not self.max_size or not self.persist_path.exists():
            return
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        loaded: OrderedDict[str, tuple[float, str, str]] = OrderedDict()
        for path, item in entries.items():
            if not isinstance(path, str) or not isinstance(item, list) or len(item) < 3:
                continue
            saved_at, url, thumb = item[0], item[1], item[2]
            if not isinstance(saved_at, (int, float)) or not isinstance(url, str) or not isinstance(thumb, str):
                continue
            age = now_wall - float(saved_at)
            if age < 0 or age >= self.ttl_seconds or not (url or thumb):
                continue
            loaded[path] = (now_mono - age, url, thumb)
            if len(loaded) >= self.max_size:
                break
        if loaded:
            self._entries = loaded

    def _mark_dirty(self) -> None:
        if not self.persist_path or not self.max_size:
            return
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            timer = threading.Timer(1.5, self._flush)
            timer.daemon = True
            self._save_timer = timer
            timer.start()

    def _flush(self) -> None:
        if not self.persist_path or not self.max_size or not self.persist_path.parent.exists():
            return
        now_wall = time.time()
        now_mono = time.monotonic()
        with self._lock:
            payload = {
                "saved_at": int(now_wall),
                "entries": {
                    path: [now_wall - (now_mono - stamp), url, thumb]
                    for path, (stamp, url, thumb) in self._entries.items()
                    if now_mono - stamp < self.ttl_seconds and (url or thumb)
                },
            }
        try:
            atomic_write_json(self.persist_path, payload)
        except OSError:
            logging.debug("unable to persist url cache")

    def remember(self, path: str, url: str = "", thumb: str = "") -> None:
        if not self.max_size or not (url or thumb):
            return
        with self._lock:
            previous = self._entries.get(path)
            if previous and time.monotonic() - previous[0] < self.ttl_seconds:
                url = url or previous[1]
                thumb = thumb or previous[2]
            self._entries[path] = (time.monotonic(), url, thumb)
            self._entries.move_to_end(path)
            while len(self._entries) > self.max_size:
                self._entries.popitem(last=False)
        self._mark_dirty()

    def resolve(self, path: str, client: OpenListClient, refresh: bool = False) -> tuple[str, str]:
        if not refresh:
            cached = self._cached(path)
            if cached is not None and cached[0]:
                return cached
        key = (path, "download")
        with self._lock:
            inflight = self._inflight.get(key)
            if inflight is None:
                if not refresh:
                    cached = self._cached_unlocked(path)
                    if cached is not None and cached[0]:
                        return cached
                inflight = _InflightResolve()
                self._inflight[key] = inflight
                self.misses += 1
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
                    previous = self._entries.get(path)
                    if previous and time.monotonic() - previous[0] < self.ttl_seconds:
                        thumb = thumb or previous[2]
                    self._entries[path] = (time.monotonic(), url, thumb)
                    self._entries.move_to_end(path)
                    while len(self._entries) > self.max_size:
                        self._entries.popitem(last=False)
                self._mark_dirty()
            inflight.result = (url, thumb)
            return url, thumb
        except Exception as error:
            inflight.error = error
            raise
        finally:
            with self._lock:
                if self._inflight.get(key) is inflight:
                    del self._inflight[key]
            inflight.event.set()

    def resolve_preview(self, path: str, client: OpenListClient, refresh: bool = False) -> tuple[str, str]:
        if not refresh:
            cached = self._cached(path)
            if cached is not None and cached[1]:
                return "", cached[1]
        key = (path, "preview")
        with self._lock:
            inflight = self._inflight.get(key)
            if inflight is None:
                if not refresh:
                    cached = self._cached_unlocked(path)
                    if cached is not None and cached[1]:
                        return "", cached[1]
                inflight = _InflightResolve()
                self._inflight[key] = inflight
                self.misses += 1
                leader = True
            else:
                leader = False
        if not leader:
            inflight.event.wait()
            if inflight.error is not None:
                raise inflight.error
            if inflight.result is None:
                raise RuntimeError("preview resolve produced no result")
            return inflight.result
        try:
            _url, thumb = client.resolve_preview(path)
            if self.max_size:
                with self._lock:
                    previous = self._entries.get(path)
                    url = previous[1] if previous and time.monotonic() - previous[0] < self.ttl_seconds else ""
                    self._entries[path] = (time.monotonic(), url, thumb)
                    self._entries.move_to_end(path)
                    while len(self._entries) > self.max_size:
                        self._entries.popitem(last=False)
                self._mark_dirty()
            inflight.result = ("", thumb)
            return "", thumb
        except Exception as error:
            inflight.error = error
            raise
        finally:
            with self._lock:
                if self._inflight.get(key) is inflight:
                    del self._inflight[key]
            inflight.event.set()

    def status(self) -> dict[str, int]:
        with self._lock:
            return {"size": len(self._entries), "hits": self.hits, "misses": self.misses}


class SharedImageChain:
    def __init__(self, length: int = SHARED_CHAIN_LENGTH, ttl_seconds: int = SHARED_CHAIN_TTL_SECONDS) -> None:
        self.length = max(1, int(length))
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._lock = threading.Lock()
        self._fingerprint: tuple[int, int] | None = None
        self._order: list[int] = []
        self._current: dict[str, Any] | None = None
        self._previous: dict[str, Any] | None = None
        self._generation = 0

    def _ensure_order(self, size: int, generated_at: int) -> None:
        generated_at = int(generated_at or 1)
        fingerprint = (generated_at, size)
        if self._fingerprint == fingerprint:
            return
        order = list(range(size))
        random.Random(generated_at).shuffle(order)
        self._order = order
        self._fingerprint = fingerprint
        self._current = None
        self._previous = None
        self._generation = 0

    def _window_id(self, wall: float | None = None) -> int:
        return int(wall if wall is not None else time.time()) // self.ttl_seconds

    def _seconds_until_window_end(self, wall: float | None = None) -> float:
        wall = float(wall if wall is not None else time.time())
        window_end = (self._window_id(wall) + 1) * self.ttl_seconds
        return max(1.0, window_end - wall)

    def _new_chain(self, size: int, now: float) -> dict[str, Any]:
        chain_length = min(self.length, size)
        start = (self._generation * chain_length) % size
        window_id = self._window_id()
        self._generation += 1
        return {
            "id": f"{self._fingerprint[0]}-{window_id}-{self._generation}",
            "window_id": window_id,
            "start": start,
            "length": chain_length,
            "high_water": 0,
            "expires_at": now + self._seconds_until_window_end(),
        }

    def _rotate(self, size: int, now: float) -> None:
        self._previous = self._current if self._alive(self._current, now) else None
        self._current = self._new_chain(size, now)

    def _alive(self, chain: dict[str, Any] | None, now: float) -> bool:
        return bool(chain) and now < float(chain["expires_at"])

    def status(self) -> dict[str, Any]:
        with self._lock:
            current = self._current
            previous = self._previous
            payload: dict[str, Any] = {"length": self.length, "ttl_seconds": self.ttl_seconds}
            if current:
                payload.update(
                    {
                        "id": current["id"],
                        "offset": int(current["high_water"]),
                        "remaining": max(0, int(current["length"]) - int(current["high_water"])),
                    }
                )
            if previous:
                payload["previous_id"] = previous["id"]
            return payload

    def slice(self, size: int, generated_at: int, count: int, chain_id: str | None = None, offset: int | None = None) -> tuple[list[int], dict[str, Any]]:
        if size <= 0 or count <= 0:
            return [], {"id": "", "offset": 0, "remaining": 0, "length": 0, "rotated": False}
        now = time.monotonic()
        requested_offset = max(0, int(offset or 0))
        with self._lock:
            self._ensure_order(size, generated_at)
            rotated = False
            if self._previous is not None and not self._alive(self._previous, now):
                self._previous = None
            if self._current is None or not self._alive(self._current, now) or int(self._current.get("window_id", -1)) != self._window_id():
                self._rotate(size, now)
                rotated = True
            previous = self._previous
            current = self._current
            if chain_id and previous and chain_id == previous["id"] and self._alive(previous, now) and requested_offset < int(previous["length"]):
                target = previous
                position = requested_offset
            elif chain_id and current and chain_id == current["id"] and requested_offset < int(current["length"]):
                target = current
                position = requested_offset
            else:
                if int(current["high_water"]) >= int(current["length"]):
                    self._rotate(size, now)
                    rotated = True
                    current = self._current
                target = current
                position = 0
            take_count = min(count, int(target["length"]) - position)
            start = int(target["start"])
            positions = [self._order[(start + position + index) % size] for index in range(take_count)]
            target["high_water"] = max(int(target["high_water"]), position + take_count)
            info = {
                "id": target["id"],
                "offset": position + take_count,
                "remaining": int(target["length"]) - (position + take_count),
                "length": int(target["length"]),
                "rotated": rotated,
            }
            return positions, info


class Application:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.repository = IndexRepository(Path(self.config["state_dir"]))
        self.tags = TagRepository(Path(self.config["state_dir"]))
        self.cache = self._make_url_cache()
        self.shared_chain = SharedImageChain()
        self.url_executor = ThreadPoolExecutor(max_workers=URL_RESOLVE_WORKERS, thread_name_prefix="openlist-url")
        self.config_lock = threading.Lock()
        self.refresh_lock = threading.Lock()
        self.refreshing = False
        self.last_refresh_error = ""
        self.index_progress_lock = threading.Lock()
        self.index_progress: dict[str, Any] = {
            "completed": 0,
            "queued": 0,
            "active": 0,
            "failed": 0,
            "directory_count": 0,
            "image_count": 0,
            "elapsed_seconds": 0.0,
            "retry_round": False,
            "complete": False,
        }

    def _make_url_cache(self) -> UrlCache:
        persist = Path(self.config["state_dir"]) / "url_cache.json"
        return UrlCache(self.config["url_cache_size"], self.config["url_cache_ttl_seconds"], persist)

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
            self.cache = self._make_url_cache()
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
        personal = {
            "label": self.config["contact_personal_label"] or "个人",
            "url": self.config["contact_personal_url"] if self.config["contact_enabled"] else "",
            "image": self.config["contact_personal_image"] if self.config["contact_enabled"] else "",
        }
        group = {
            "label": self.config["contact_group_label"] or "群组",
            "url": self.config["contact_group_url"] if self.config["contact_enabled"] else "",
            "image": self.config["contact_group_image"] if self.config["contact_enabled"] else "",
        }
        has_personal = bool(personal["url"] or personal["image"])
        has_group = bool(group["url"] or group["image"])
        config["contact"] = {
            "enabled": self.config["contact_enabled"] and (has_personal or has_group),
            "label": self.config["contact_label"],
            "personal": personal if has_personal else None,
            "group": group if has_group else None,
        }
        config["maintenance_enabled"] = self.config["maintenance_enabled"]
        config["filter_enabled"] = self.config["filter_enabled"]
        config["tagging"] = {
            "enabled": self.config["tagging_enabled"] and self.config["tagging_scope"] != "disabled",
            "scope": self.config["tagging_scope"],
            "categories": self.config["tagging_categories"],
            "allow_custom": self.config["tagging_allow_custom"],
            "sort_default": self.config["tagging_sort_default"],
            "trash_tag": TRASH_TAG,
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
            "contact_enabled": self.config["contact_enabled"],
            "contact_label": self.config["contact_label"],
            "contact_personal_label": self.config["contact_personal_label"],
            "contact_personal_url": self.config["contact_personal_url"],
            "contact_personal_image": self.config["contact_personal_image"],
            "contact_group_label": self.config["contact_group_label"],
            "contact_group_url": self.config["contact_group_url"],
            "contact_group_image": self.config["contact_group_image"],
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
            "contact_enabled",
            "contact_label",
            "contact_personal_label",
            "contact_personal_url",
            "contact_personal_image",
            "contact_group_label",
            "contact_group_url",
            "contact_group_image",
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
                "contact_enabled": self.config["contact_enabled"],
                "contact_label": self.config["contact_label"],
                "contact_personal_label": self.config["contact_personal_label"],
                "contact_personal_url": self.config["contact_personal_url"],
                "contact_personal_image": self.config["contact_personal_image"],
                "contact_group_label": self.config["contact_group_label"],
                "contact_group_url": self.config["contact_group_url"],
                "contact_group_image": self.config["contact_group_image"],
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
            "contact_enabled",
            "contact_label",
            "contact_personal_label",
            "contact_personal_url",
            "contact_personal_image",
            "contact_group_label",
            "contact_group_url",
            "contact_group_image",
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

    def _set_index_progress(self, value: dict[str, Any]) -> None:
        with self.index_progress_lock:
            self.index_progress = dict(value)

    def start_refresh(self) -> bool:
        if not self.refresh_lock.acquire(blocking=False):
            return False
        self.refreshing = True
        self._set_index_progress({
            "completed": 0,
            "queued": len(self.config["directories"]),
            "active": 0,
            "failed": 0,
            "directory_count": 0,
            "image_count": 0,
            "elapsed_seconds": 0.0,
            "retry_round": False,
            "complete": False,
        })

        def worker() -> None:
            try:
                index = build_index(self.config, self.repository, self._set_index_progress)
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
                with self.index_progress_lock:
                    self.index_progress = {**self.index_progress, "complete": True}
                self.refresh_lock.release()

        threading.Thread(target=worker, name="openlist-index-rebuild", daemon=True).start()
        return True

    def status(self) -> dict[str, Any]:
        try:
            index = self.repository.load()
        except RuntimeError as error:
            index = {"images": [], "directory_count": 0, "generated_at": 0, "errors": [str(error)]}
        progress_lock = getattr(self, "index_progress_lock", None)
        if progress_lock is None:
            progress = {}
        else:
            with progress_lock:
                progress = dict(getattr(self, "index_progress", {}))
        return {
            "status": "ok",
            "image_count": len(index.get("images", [])),
            "directory_count": int(index.get("directory_count") or 0),
            "generated_at": int(index.get("generated_at") or 0),
            "last_build_duration_seconds": float(index.get("build_duration_seconds") or 0),
            "refreshing": self.refreshing,
            "last_refresh_error": self.last_refresh_error,
            "index_progress": progress,
            "cache": self.cache.status(),
            "shared_chain": self.shared_chain_status(),
            **self.public_config(),
        }

    def list_directories(self, path: str) -> list[dict[str, Any]]:
        """List subdirectories of a virtual path live from OpenList.

        BaiduPhoto mounts (一刻相册) are selectable storage roots only: their
        hundreds of author albums are not expanded in the admin tree.
        """
        directory = normalize_directory(path)
        client = OpenListClient(self.config)
        baidu_mounts = discover_baidu_photo_mounts(client, list(self.config["directories"]) + [directory])
        mode = baidu_photo_scan_mode(directory, baidu_mounts)
        if mode in {"mount", "inside"}:
            # Do not expand BaiduPhoto mount children in the admin picker.
            return []
        try:
            entries = client.list_directory(directory)
        except Exception as error:
            if directory != "/":
                raise RuntimeError(f"unable to list directory {directory}: {error}") from error
            roots = [
                {
                    "name": item.rsplit("/", 1)[-1] or "/",
                    "path": item,
                    "has_children": baidu_photo_scan_mode(item, baidu_mounts) == "normal",
                    "storage_driver": BAIDU_PHOTO_DRIVER if baidu_photo_scan_mode(item, baidu_mounts) != "normal" else "",
                    "leaf": baidu_photo_scan_mode(item, baidu_mounts) != "normal",
                }
                for item in self.config["directories"]
            ]
            return sorted(roots, key=lambda item: item["name"].casefold())
        if directory == "/":
            try:
                baidu_mounts |= client.baidu_photo_mounts()
            except Exception:
                pass
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
            child_mode = baidu_photo_scan_mode(child_path, baidu_mounts)
            if child_mode == "normal" and directory == "/":
                # Root entries may be storage mounts not yet known; probe provider once.
                try:
                    if client.path_provider(child_path) == BAIDU_PHOTO_DRIVER:
                        baidu_mounts.add(child_path)
                        child_mode = "mount"
                except Exception:
                    pass
            leaf = child_mode != "normal"
            children.append(
                {
                    "name": name,
                    "path": child_path,
                    "has_children": not leaf,
                    "storage_driver": BAIDU_PHOTO_DRIVER if leaf else "",
                    "leaf": leaf,
                }
            )
        children.sort(key=lambda item: item["name"].casefold())
        return children

    def shared_chain_status(self) -> dict[str, Any]:
        chain = getattr(self, "shared_chain", None)
        if chain is None:
            return {}
        return chain.status()

    def choose_images(self, count: int, folder: str | None, min_size: int | None, max_size: int | None, tags: list[str] | None = None, filter_mode: str = "union", offset: int | None = None, chain_id: str | None = None, use_shared_chain: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
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
            return [], None
        count = max(1, min(count, 50))
        unfiltered = not folder and min_size is None and max_size is None and not tags
        chain = getattr(self, "shared_chain", None)
        if use_shared_chain and unfiltered and chain is not None:
            generated_at = int(index.get("generated_at") or 0)
            positions, info = chain.slice(len(images), generated_at, count, chain_id=chain_id, offset=offset)
            return [images[position] for position in positions], info
        if count <= len(images):
            return random.sample(images, count), None
        return [random.choice(images) for _ in range(count)], None

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
        return self._collect_url_resolves(images, resolve_one)

    def resolve_preview_urls(self, paths: list[str], refresh: bool = False) -> list[dict[str, Any]]:
        images = self.indexed_images(paths[:50])
        if not images:
            return []
        client = OpenListClient(self.config)

        def resolve_one(image: dict[str, Any]) -> dict[str, Any]:
            path = str(image["path"])
            if image.get("_missing"):
                return {"path": path, "error": "image is not in the current index"}
            try:
                url, thumb = self.cache.resolve_preview(path, client, refresh=refresh)
            except Exception:
                logging.warning("Failed to resolve preview URL for %s", path)
                return {"path": path, "error": "unable to resolve image URL"}
            return {"path": path, "url": url, "thumbnail": thumb or ""}

        if len(images) == 1:
            return [resolve_one(images[0])]
        return self._collect_url_resolves(images, resolve_one)

    def _collect_url_resolves(self, images: list[dict[str, Any]], resolve_one) -> list[dict[str, Any]]:
        futures = {self.url_executor.submit(resolve_one, image): str(image["path"]) for image in images}
        done, _not_done = wait(futures, timeout=URL_RESOLVE_WAIT_SECONDS)
        completed: dict[str, dict[str, Any]] = {}
        for future in done:
            path = futures[future]
            try:
                completed[path] = future.result()
            except Exception:
                logging.warning("Failed to resolve URL for %s", path)
                completed[path] = {"path": path, "error": "unable to resolve image URL"}
        return [
            completed.get(str(image["path"]))
            or {"path": str(image["path"]), "error": "url resolve timed out"}
            for image in images
        ]

    def resolve_images_lazy(self, images: list[dict[str, Any]], include_tags: bool = False) -> list[dict[str, Any]]:
        paths = [str(image["path"]) for image in images]
        tag_stats = self.tags.stats(paths) if include_tags else {}
        results = []
        for image in images:
            path = str(image["path"])
            cached = self.cache.cached_pair(path)
            url = cached[0] if cached else ""
            thumb = cached[1] if cached else ""
            result = {"path": path, "size": int(image.get("size") or 0), "url": url, "thumbnail": thumb, "needs_url": not thumb and not url}
            if include_tags:
                result["tags"] = tag_stats.get(path, {"likes": 0, "dislikes": 0, "categories": []})
            results.append(result)
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

    TRASH_TAG = TRASH_TAG

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


_WEBUI_CACHE: dict[str, str] = {}


def webui_dir() -> Path:
    return Path(__file__).resolve().parent / "webui"


def load_webui(name: str) -> str:
    cached = _WEBUI_CACHE.get(name)
    if cached is not None:
        return cached
    path = webui_dir() / name
    if not path.is_file():
        raise FileNotFoundError(f"missing webui asset: {path}")
    text = path.read_text(encoding="utf-8")
    _WEBUI_CACHE[name] = text
    return text


def gallery_html() -> str:
    return load_webui("gallery.html")


def admin_html() -> str:
    return load_webui("admin.html")


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
                    offset_raw = params.get("offset", [None])[0]
                    offset = None if offset_raw in (None, "") else self._query_int(params, "offset", 0)
                    chain_id = (params.get("chain", [""])[0] or "").strip() or None
                    tag_filter = params.get("tag", []) or params.get("tags", [])
                    filter_mode = params.get("filter_mode", ["union"])[0]
                    use_shared_chain = not tag_filter and not params.get("folder", [None])[0] and not params.get("min_size", [None])[0] and not params.get("max_size", [None])[0]
                    images, chain_info = application.choose_images(
                        count, params.get("folder", [None])[0], parse_size(params.get("min_size", [None])[0]), parse_size(params.get("max_size", [None])[0]), tags=tag_filter or None, filter_mode=filter_mode, offset=offset, chain_id=chain_id, use_shared_chain=use_shared_chain
                    )
                    if not images:
                        logging.debug("images/random: count=%d tags=%s mode=%s -> 0 images in %.3fs", count, tag_filter, filter_mode, time.time() - request_start)
                        payload = {"images": []}
                        if chain_info:
                            payload["chain"] = chain_info
                        return self._send_json(HTTPStatus.OK, payload)
                    include_tags = application.config["tagging_enabled"] and application.config["tagging_scope"] != "disabled"
                    result = application.resolve_images_lazy(images, include_tags=include_tags)
                    cached = sum(1 for r in result if not r.get("needs_url"))
                    logging.debug("images/random: count=%d tags=%s mode=%s -> %d images (%d cached) in %.3fs", count, tag_filter, filter_mode, len(result), cached, time.time() - request_start)
                    payload = {"images": result}
                    if chain_info:
                        payload["chain"] = chain_info
                    return self._send_json(HTTPStatus.OK, payload)
                if parsed.path == "/api/download-url":
                    if self._maintenance_access_required():
                        return
                    raw_path = params.get("path", [""])[0]
                    refresh = params.get("fresh", ["0"])[0].lower() in {"1", "true", "yes"}
                    preview = params.get("preview", ["0"])[0].lower() in {"1", "true", "yes"}
                    image = application.indexed_image(raw_path)
                    if preview:
                        resolved = application.resolve_preview_urls([raw_path], refresh=refresh)[0]
                    else:
                        resolved = application.resolve_images([image], refresh=refresh)[0]
                    if resolved.get("error"):
                        return self._send_json(HTTPStatus.BAD_GATEWAY, {"error": resolved["error"]})
                    return self._send_json(HTTPStatus.OK, {"url": resolved.get("url", ""), "thumbnail": resolved.get("thumbnail", "")})
                if parsed.path == "/download":
                    if self._maintenance_access_required():
                        return
                    raw_path = params.get("path", [""])[0]
                    image = application.indexed_image(raw_path)
                    return self._proxy_download(image)
                if parsed.path == "/random":
                    if self._maintenance_access_required():
                        return
                    images, _chain = application.choose_images(1, params.get("folder", [None])[0], None, None)
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
                preview = payload.get("preview")
                if isinstance(preview, str):
                    preview = preview.lower() in {"1", "true", "yes"}
                else:
                    preview = bool(preview)
                resolved = (
                    application.resolve_preview_urls(paths, refresh=refresh)
                    if preview
                    else application.resolve_download_urls(paths, refresh=refresh)
                )
                return self._send_json(HTTPStatus.OK, {"images": resolved})
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
                    is_trash = category in {application.TRASH_TAG, LEGACY_TRASH_TAG}
                    if is_trash:
                        category = application.TRASH_TAG
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
