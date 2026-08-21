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
    "contact_qq_number": "",
    "contact_qq_url": "",
    "contact_qr_url": "",
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
URL_RESOLVE_WAIT_SECONDS = 8
INDEX_LIST_TIMEOUT_SECONDS = 10
INDEX_LIST_WORKERS = 4
INDEX_CHECKPOINT_INTERVAL = 32
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
    if not isinstance(config["contact_enabled"], bool):
        raise ValueError("contact_enabled must be a boolean")
    if not isinstance(config["contact_label"], str):
        raise ValueError("contact_label must be a string")
    config["contact_label"] = config["contact_label"].strip() or "联系"
    if len(config["contact_label"]) > 20:
        raise ValueError("contact_label is too long")
    if not isinstance(config["contact_qq_number"], str):
        raise ValueError("contact_qq_number must be a string")
    config["contact_qq_number"] = config["contact_qq_number"].strip()
    if config["contact_qq_number"] and not re.fullmatch(r"\d{5,12}", config["contact_qq_number"]):
        raise ValueError("contact_qq_number must be 5-12 digits")
    if not isinstance(config["contact_qq_url"], str):
        raise ValueError("contact_qq_url must be a string")
    config["contact_qq_url"] = config["contact_qq_url"].strip()
    if config["contact_qq_url"]:
        parsed_contact = urlparse(config["contact_qq_url"])
        if parsed_contact.scheme not in {"http", "https"} or not parsed_contact.netloc:
            raise ValueError("contact_qq_url must be an http or https URL")
        if len(config["contact_qq_url"]) > 300:
            raise ValueError("contact_qq_url is too long")
    if not isinstance(config["contact_qr_url"], str):
        raise ValueError("contact_qr_url must be a string")
    config["contact_qr_url"] = config["contact_qr_url"].strip()
    if config["contact_qr_url"]:
        parsed_qr = urlparse(config["contact_qr_url"])
        if parsed_qr.scheme not in {"http", "https"} or not parsed_qr.netloc:
            raise ValueError("contact_qr_url must be an http or https URL")
        if len(config["contact_qr_url"]) > 300:
            raise ValueError("contact_qr_url is too long")
    if config["contact_enabled"] and not config["contact_qq_number"] and not config["contact_qq_url"]:
        raise ValueError("contact requires a QQ number or a desktop URL")
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

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        timeout: float = 15,
        retries: int = 1,
        retry_throttled_only: bool = False,
    ) -> dict[str, Any]:
        token = read_secret_cached(self.token_path, "OpenList API token")
        post_start = time.time()
        request = Request(
            f"{self.base_url}{endpoint}",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": token, "Content-Type": "application/json"},
            method="POST",
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
                logging.debug("OpenList %s: %.3fs (attempt %d)", endpoint, time.time() - post_start, attempt + 1)
                return data
            except (HTTPError, URLError, TimeoutError, ValueError, RuntimeError) as error:
                last_error = error
                logging.debug("OpenList %s failed attempt %d: %s", endpoint, attempt + 1, error)
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
            if total < len(entries) or not content:
                return entries
            page += 1

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
        for entry in entries:
            name = str(entry.get("name") or "")
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                continue
            path = join_virtual_path(current, name)
            if entry.get("is_dir"):
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


class Application:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = load_config(config_path)
        self.repository = IndexRepository(Path(self.config["state_dir"]))
        self.tags = TagRepository(Path(self.config["state_dir"]))
        self.cache = self._make_url_cache()
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
        config["contact"] = {
            "enabled": self.config["contact_enabled"] and bool(self.config["contact_qq_number"] or self.config["contact_qq_url"]),
            "label": self.config["contact_label"],
            "qq_number": self.config["contact_qq_number"] if self.config["contact_enabled"] else "",
            "qq_url": self.config["contact_qq_url"] if self.config["contact_enabled"] else "",
            "qr_url": self.config["contact_qr_url"] if self.config["contact_enabled"] else "",
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
            "contact_qq_number": self.config["contact_qq_number"],
            "contact_qq_url": self.config["contact_qq_url"],
            "contact_qr_url": self.config["contact_qr_url"],
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
            "contact_qq_number",
            "contact_qq_url",
            "contact_qr_url",
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
                "contact_qq_number": self.config["contact_qq_number"],
                "contact_qq_url": self.config["contact_qq_url"],
                "contact_qr_url": self.config["contact_qr_url"],
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
            "contact_qq_number",
            "contact_qq_url",
            "contact_qr_url",
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
        futures = {self.url_executor.submit(resolve_one, image): str(image["path"]) for image in images}
        done, not_done = wait(futures, timeout=URL_RESOLVE_WAIT_SECONDS)
        for future in not_done:
            future.cancel()
        completed = {futures[future]: future.result() for future in done}
        return [
            completed.get(str(image["path"]))
            or {"path": str(image["path"]), "error": "url resolve timed out"}
            for image in images
        ]

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
        futures = {self.url_executor.submit(resolve_one, image): str(image["path"]) for image in images}
        done, not_done = wait(futures, timeout=URL_RESOLVE_WAIT_SECONDS)
        for future in not_done:
            future.cancel()
        completed = {futures[future]: future.result() for future in done}
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


def gallery_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>图库</title>
<style>
:root{
  color-scheme:dark;
  --bg:#07080c;
  --bg-elev:#0e1218;
  --bg-soft:#141922;
  --line:#1f2633;
  --text:#d7deea;
  --muted:#7b879b;
  --accent:#3d8bfd;
  --accent-hover:#5aa0ff;
  --danger:#ff5d6c;
  --radius:14px;
  --radius-sm:9px;
  --shadow:0 16px 40px rgba(0,0,0,.5);
  --header-h:56px;
  --font:"Segoe UI",system-ui,-apple-system,"PingFang SC","Noto Sans SC",sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;min-height:100%;background:var(--bg);color:var(--text);font:15px/1.45 var(--font)}
button,.button,input,select,textarea{font:inherit}
button,.button{border:0;border-radius:var(--radius-sm);background:var(--accent);color:#fff;padding:8px 13px;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px}
button:hover,.button:hover{background:var(--accent-hover)}
button:disabled{opacity:.45;cursor:not-allowed}
button.ghost,.button.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
button.ghost:hover,.button.ghost:hover{background:var(--bg-soft);border-color:var(--accent)}
button:focus-visible,.button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:2px}
.meta{color:var(--muted);font-size:13px}
.hidden{display:none!important}
a{color:inherit}
header{
  position:sticky;z-index:8;top:0;display:flex;align-items:center;gap:12px;
  min-height:var(--header-h);padding:8px 16px;
  background:color-mix(in srgb,var(--bg) 86%,transparent);
  backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);
  border-bottom:1px solid var(--line);
}
.brand{display:flex;align-items:baseline;gap:10px;min-width:0;flex:1}
.brand strong{font-size:16px;letter-spacing:.02em}
.brand .meta{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.header-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.header-actions #previous,.header-actions #next,.header-actions #slideshow-toggle{display:none!important}
#header-menu-toggle{
  display:none;width:40px;height:40px;padding:0;border-radius:11px;
  background:var(--bg-soft);border:1px solid var(--line);color:var(--text);font-size:13px
}
.header-menu{
  position:fixed;z-index:6;top:var(--menu-top,56px);right:10px;width:188px;
  display:flex;flex-direction:column;gap:8px;padding:12px;
  background:var(--bg-elev);border:1px solid var(--line);border-radius:16px;
  box-shadow:var(--shadow);
  opacity:0;visibility:hidden;transform:translateY(-8px);
  transition:opacity .22s ease,transform .22s ease,visibility .22s
}
.header-menu.open{opacity:1;visibility:visible;transform:none}
#header-menu-backdrop{position:fixed;z-index:5;inset:0;opacity:0;visibility:hidden;background:#0007;transition:opacity .22s ease,visibility .22s}
#header-menu-backdrop.open{opacity:1;visibility:visible}
#header-menu .button,#header-menu button,#header-menu a{
  width:100%;text-align:center;padding:11px 12px;border-radius:10px
}
#menu-previous,#menu-next,#menu-slideshow-toggle{display:none!important}
.gallery{padding:16px}
.gallery.waterfall{display:flex;align-items:flex-start;gap:var(--grid-gap,12px);width:min(100%,1800px);margin:0 auto}
.waterfall-column{display:flex;min-width:0;flex:1;flex-direction:column;gap:var(--grid-gap,12px)}
.gallery.slideshow{display:grid;min-height:calc(100dvh - var(--header-h) - 118px);place-items:center;touch-action:pan-y}
.gallery.slideshow .card{max-width:min(96vw,1280px);width:100%}
.gallery.waterfall .card{min-height:0}
.gallery.waterfall .card img{max-height:none;min-height:0;height:auto}
.card{position:relative;background:var(--bg-elev);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease}
@media(hover:hover){.card:hover{transform:translateY(-3px);box-shadow:var(--shadow);border-color:var(--accent)}}
.preview-button{display:block;width:100%;padding:0;border:0;border-radius:0;background:transparent;cursor:zoom-in}
.card img{width:100%;display:block;max-height:82vh;object-fit:contain;background:#05060a;color:transparent;font-size:0}
.card.is-loading img,.card img:not([src]),.card img[src=""]{min-height:180px;visibility:hidden}
.card.is-loading .card-tags{display:none}
.image-error{display:grid;min-height:180px;place-items:center;gap:8px;padding:24px;text-align:center;color:var(--muted)}
.image-error button{justify-self:center}
#empty{padding:48px 20px;text-align:center;color:var(--muted)}
dialog{width:min(96vw,1500px);height:min(94vh,1000px);padding:0;border:1px solid var(--line);border-radius:16px;background:var(--bg);color:var(--text)}
dialog::backdrop{background:#000c}
.lightbox-head,.lightbox-foot{display:flex;gap:12px;align-items:center;padding:10px 12px}
.lightbox-head{justify-content:space-between}
.lightbox-controls{display:flex;gap:8px}
.lightbox-stage{position:relative;height:calc(94vh - 126px);overflow:hidden;background:#080a0f;display:grid;place-items:center;cursor:default;touch-action:none;overscroll-behavior:contain}
.lightbox-stage.can-pan{cursor:grab}
.lightbox-stage.can-pan.is-dragging{cursor:grabbing}
.lightbox-image{display:block;width:auto;height:auto;max-width:none;max-height:none;object-fit:fill;transform-origin:center;transition:transform .15s ease;pointer-events:none;user-select:none;will-change:transform}
.lightbox-meta{min-width:0;display:grid;gap:4px}
.lightbox-caption,.lightbox-directory{margin:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lightbox-directory{color:var(--muted);font-size:13px}
.modal-backdrop{position:fixed;z-index:4;inset:0;background:#0008}
#announcement-backdrop{position:fixed!important;z-index:1000!important;inset:0!important;background:#10131a!important;opacity:1!important;pointer-events:auto!important;animation:backdrop-in .25s ease both}
.announcement{z-index:1001!important;pointer-events:auto!important}
body.announcement-open{overflow:hidden}
body.announcement-open>header,body.announcement-open>main,body.announcement-open>#maintenance{pointer-events:none!important;user-select:none}
body.announcement-open>#announcement-backdrop,body.announcement-open>#announcement{display:block!important;visibility:visible!important}
.preferences{
  position:fixed;z-index:5;top:50%;left:50%;width:min(92vw,460px);max-height:86vh;overflow:auto;
  padding:22px;border:1px solid var(--line);border-radius:18px;background:var(--bg-elev);color:var(--text);
  transform:translate(-50%,-50%);box-shadow:var(--shadow)
}
.preferences h2{margin:0 0 8px;font-size:18px}
.pref-section{margin:14px 0;padding-top:12px;border-top:1px solid var(--line)}
.pref-section h3{margin:0 0 8px;font-size:13px;color:var(--muted);font-weight:600}
.preferences label{display:grid;gap:6px;margin:10px 0}
.preferences select,.preferences input{padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)}
.preferences-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:2px 14px}
.preferences .check{display:flex;align-items:center;gap:8px;margin:12px 0}
.preferences .check input{width:auto;margin:0}
.preferences-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}
.announcement{position:fixed;z-index:5;top:50%;left:50%;width:min(94vw,680px);max-height:min(78vh,680px);height:auto;padding:0;border:0;border-radius:22px;background:linear-gradient(145deg,#fffdf8 0%,#fff 54%,#fff0e3 100%);color:#282828;box-shadow:0 1.5rem 3rem rgba(0,0,0,.38);overflow:hidden;transform:translate(-50%,-50%);animation:announcement-in .32s cubic-bezier(.2,.8,.2,1) both;display:flex;flex-direction:column}
.announcement.is-closing{animation:announcement-out .22s ease-in both}
@keyframes backdrop-in{from{opacity:0}to{opacity:1}}
@keyframes announcement-in{from{opacity:0;transform:translate(-50%,-46%) scale(.96)}to{opacity:1;transform:translate(-50%,-50%) scale(1)}}
@keyframes announcement-out{from{opacity:1;transform:translate(-50%,-50%) scale(1)}to{opacity:0;transform:translate(-50%,-46%) scale(.97)}}
.announcement-main{padding:26px 30px 12px;display:flex;min-height:0;flex:1;flex-direction:column}
.announcement-title{position:relative;z-index:0;display:inline-block;margin:0 0 18px;font-size:21px}
.announcement-title::after{content:'';position:absolute;z-index:-1;right:-3px;bottom:2px;left:-3px;height:14px;border-radius:4px;background:#fbeecd;transform:skewX(-15deg)}
.announcement-content{margin:0;line-height:1.7;min-height:0;overflow-y:auto;overscroll-behavior:contain;scrollbar-gutter:stable;padding-right:8px}
.announcement-content h1,.announcement-content h2,.announcement-content h3,.announcement-content h4{margin:16px 0 8px}
.announcement-content p{margin:8px 0}
.announcement-content code{padding:2px 5px;border-radius:4px;background:#f3f5f7}
.announcement-content pre{overflow:auto;padding:12px;border-radius:8px;background:#f3f5f7}
.announcement-content pre code{padding:0}
.announcement-content a{color:#b63813}
.announcement-content img{display:block;max-width:100%;height:auto;margin:12px auto;border-radius:10px}
.announcement-actions{display:flex;justify-content:center;gap:10px;flex-wrap:wrap}
.announcement-footer{padding:12px 30px 28px;text-align:center;background:linear-gradient(170deg,#fff 0%,#fff 38%,#fbeecd 100%);flex:0 0 auto}
.announcement-footer button{border-radius:50px;background:linear-gradient(to right,#ff711f,#e50914);box-shadow:0 10px 12px -4px rgba(229,9,20,.25)}
body.announcement-open>#announcement{display:flex!important}
.maintenance{max-width:520px;margin:13vh auto;padding:28px;border:1px solid var(--line);border-radius:18px;background:var(--bg-elev);text-align:center}
.maintenance details{text-align:left;margin-top:22px}
.maintenance label{display:grid;gap:7px;margin:14px 0}
.maintenance input{padding:9px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);width:100%}
.slide-history{padding:8px 16px calc(12px + env(safe-area-inset-bottom));border-top:1px solid var(--line);background:var(--bg)}
.slide-history-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:7px}
.slide-history-title{color:var(--muted);font-size:13px}
.slide-history-latest{padding:6px 10px;background:var(--bg-soft);font-size:13px}
.slide-history-track{display:flex;gap:8px;overflow-x:auto;padding:2px 1px 6px;scrollbar-width:thin;scroll-snap-type:x proximity}
.slide-thumbnail{position:relative;flex:0 0 76px;width:76px;height:58px;padding:0;overflow:hidden;border:1px solid var(--line);border-radius:8px;background:#080a0f;scroll-snap-align:center}
.slide-thumbnail img{display:block;width:100%;height:100%;object-fit:cover}
.slide-thumbnail.active{border-color:#fff;box-shadow:0 0 0 2px var(--accent)}
.slide-thumbnail-index{position:absolute;right:3px;bottom:3px;min-width:19px;padding:1px 4px;border-radius:4px;background:#000b;color:#fff;font-size:11px}
#theme-fab{position:fixed;right:max(16px,env(safe-area-inset-right));bottom:max(16px,env(safe-area-inset-bottom));z-index:60;width:48px;height:48px;padding:0;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:22px;background:var(--bg-elev);border:1px solid var(--line);color:var(--text);cursor:pointer;box-shadow:var(--shadow)}
#theme-fab:hover{transform:translateY(-2px);border-color:var(--accent)}
body.has-slide-history #theme-fab{bottom:calc(124px + env(safe-area-inset-bottom))}
.slide-nav{position:fixed;z-index:3;display:flex;align-items:center;justify-content:center;padding:0;border:1px solid var(--line);background:color-mix(in srgb,var(--bg) 78%,transparent);color:var(--text);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);cursor:pointer}
.slide-nav.prev{left:10px;top:50%;width:42px;height:64px;font-size:30px;border-radius:12px;transform:translateY(-50%)}
.slide-nav.next{right:10px;top:50%;width:42px;height:64px;font-size:30px;border-radius:12px;transform:translateY(-50%)}
.slide-nav.pause{left:50%;bottom:max(20px,env(safe-area-inset-bottom));min-width:64px;height:36px;padding:0 12px;font-size:13px;border-radius:18px;transform:translateX(-50%)}
body.has-slide-history .slide-nav.pause{bottom:calc(124px + env(safe-area-inset-bottom))}
.tag-bar{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:8px 16px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,rgba(0,0,0,.78) 0%,rgba(0,0,0,.38) 48%,rgba(0,0,0,.78) 100%);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px)}
.tag-bar-label{color:var(--muted);font-size:13px;margin-right:4px}
.tag-chip{display:inline-flex;align-items:center;gap:4px;padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:var(--bg-elev);color:var(--text);font-size:12px;cursor:pointer}
.tag-chip.active{background:var(--accent);border-color:var(--accent);color:#fff}
.tag-chip-count{opacity:.7;font-size:11px}
.tag-clear{padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:transparent;color:var(--muted);font-size:12px;cursor:pointer}
.card-tags{position:absolute;bottom:0;left:0;right:0;display:flex;flex-wrap:wrap;gap:5px;padding:7px 10px;background:linear-gradient(transparent,rgba(0,0,0,.65));max-height:72px;overflow:hidden}
@media(hover:hover){.card-tags{opacity:0}.card:hover .card-tags{opacity:1}.card.has-active-tag .card-tags{opacity:1}}
.tag-vote,.tag-category{display:inline-flex;align-items:center;gap:3px;padding:4px 8px;border:1px solid var(--line);border-radius:6px;background:rgba(255,255,255,.1);color:#fff;font-size:11px;cursor:pointer}
.tag-vote.active{background:#ff4d6d;border-color:#ff4d6d}
.tag-category.active{background:var(--accent);border-color:var(--accent)}
.tag-category.tag-trash{border-color:#5a3030;background:rgba(255,100,100,.12)}
.tag-category.tag-trash.active{background:#ff4757;border-color:#ff4757}
.tag-vote-count{font-weight:600;min-width:14px;text-align:center}
::-webkit-scrollbar{width:9px;height:9px}::-webkit-scrollbar-thumb{background:var(--line);border-radius:6px}::-webkit-scrollbar-track{background:transparent}
body.theme-light{color-scheme:light;--bg:#f3f5f8;--bg-elev:#fff;--bg-soft:#eef1f6;--line:#d5dbe6;--text:#1a2230;--muted:#667085;--accent:#2d6cf0;--accent-hover:#1f5ee0;--shadow:0 14px 32px rgba(30,40,60,.12)}
body.theme-light .card img,body.theme-light .lightbox-stage,body.theme-light .slide-thumbnail{background:#e8edf5}
body:not(.theme-light)::after{content:'';position:fixed;inset:0;z-index:10000;pointer-events:none;background:rgba(0,0,0,.32)}
.contact-wrap{position:relative;display:inline-flex}
#contact-popover{position:absolute;z-index:12;top:calc(100% + 8px);right:0;width:196px;padding:10px;border:1px solid var(--line);border-radius:14px;background:var(--bg-elev);box-shadow:var(--shadow);text-align:center}
#contact-popover img{display:block;width:176px;height:176px;object-fit:contain;border-radius:10px;background:#fff}
#contact-popover .meta{margin:8px 0 0}
.header-menu #contact-popover{right:auto;left:12px;top:auto;bottom:calc(100% + 8px)}
@media(max-width:760px){
  header{padding:8px 12px}
  .header-actions{display:none}
  #header-menu-toggle{display:inline-flex}
  .header-menu{left:0;right:0;width:auto;border-radius:0 0 16px 16px;border-left:0;border-right:0}
}
@media(max-width:560px){
  .gallery{padding:10px}
  .gallery.waterfall{gap:8px}
  .waterfall-column{gap:8px}
  .gallery.slideshow{min-height:calc(100dvh - var(--header-h) - 108px)}
  .brand strong{font-size:15px}
  .tag-bar{padding:5px 8px;gap:4px;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:thin;scroll-snap-type:x proximity}
  .tag-bar>*{flex:0 0 auto;scroll-snap-align:start}
  .tag-bar-label{display:none}
  .tag-chip{gap:3px;padding:3px 8px;font-size:11px}
  .tag-chip-count{font-size:10px}
  .tag-clear{padding:3px 8px;font-size:11px}
  .card-tags{max-height:48px;padding:5px 7px;gap:3px}
  .tag-vote,.tag-category{padding:3px 6px;font-size:10px}
  .preferences{width:calc(100vw - 24px)}
  .preferences-grid{grid-template-columns:1fr 1fr}
  dialog{width:100vw;height:100dvh;max-width:none;max-height:none;border:0;border-radius:0}
  .lightbox-stage{height:calc(100dvh - 126px)}
  .lightbox-foot{flex-wrap:wrap}
  .lightbox-meta{flex:1 1 100%}
  .slide-nav.prev{left:6px;width:36px;height:56px;font-size:26px}
  .slide-nav.next{right:6px;width:36px;height:56px;font-size:26px}
  .announcement{width:92vw;max-height:68vh;border-radius:14px}
  .announcement-main{padding:16px 18px 8px}
  .announcement-title{font-size:18px}
  .announcement-footer{padding:8px 18px 14px}
}
@media(max-width:400px){.preferences-grid{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;animation-duration:.01ms!important;animation-iteration-count:1!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<header>
  <div class="brand"><strong>图库</strong><span class="meta" id="status" role="status" aria-live="polite">正在加载…</span></div>
  <nav class="header-actions" aria-label="页面操作">
    <button id="previous" class="hidden ghost" type="button">上一张</button>
    <button id="slideshow-toggle" class="hidden ghost" type="button" aria-pressed="false">暂停</button>
    <button id="next" class="hidden ghost" type="button">下一张</button>
    <button id="refresh" class="ghost" type="button">刷新</button>
    <button id="settings" type="button">设置</button>
    <button id="announcement-button" class="hidden ghost" type="button">公告</button>
    <span class="contact-wrap hidden" id="contact-wrap">
      <button id="contact-button" class="ghost" type="button">联系</button>
      <div id="contact-popover" class="hidden" role="tooltip">
        <img id="contact-qr" alt="QQ 二维码">
        <p class="meta">扫码添加 QQ</p>
      </div>
    </span>
    <a href="/admin" class="button ghost">管理</a>
  </nav>
  <button id="header-menu-toggle" type="button" aria-label="菜单" aria-expanded="false">菜单</button>
</header>
<div id="header-menu-backdrop"></div>
<aside id="header-menu" class="header-menu" aria-label="快捷菜单">
  <button id="menu-previous" class="hidden ghost" type="button">上一张</button>
  <button id="menu-slideshow-toggle" class="hidden ghost" type="button" aria-pressed="false">暂停</button>
  <button id="menu-next" class="hidden ghost" type="button">下一张</button>
  <button id="menu-refresh" class="ghost" type="button">刷新</button>
  <button id="menu-settings" type="button">设置</button>
  <button id="menu-announcement" class="hidden ghost" type="button">公告</button>
  <button id="menu-contact" class="hidden ghost" type="button">联系</button>
  <a href="/admin" class="button ghost">管理</a>
</aside>
<div id="tag-bar" class="tag-bar hidden"></div>
<main id="gallery" class="gallery" aria-busy="true"></main>
<button id="slide-nav-prev" class="slide-nav prev hidden" type="button" aria-label="上一张">‹</button>
<button id="slide-nav-next" class="slide-nav next hidden" type="button" aria-label="下一张">›</button>
<button id="slide-nav-pause" class="slide-nav pause hidden" type="button" aria-label="暂停播放">暂停</button>
<section id="slide-history" class="slide-history hidden" aria-label="播放历史"><div class="slide-history-head"><span class="slide-history-title">播放历史</span><button id="slide-history-latest" class="slide-history-latest ghost" type="button">跳到最新</button></div><div id="slide-history-track" class="slide-history-track"></div></section>
<section id="maintenance" class="maintenance hidden"><h1>维护中</h1><p>图片浏览暂时不可用，请稍后再试。</p><details><summary>管理员查看图片</summary><label>管理密钥<input id="maintenance-token" type="password" autocomplete="current-password"></label><button id="maintenance-unlock" type="button">查看图片</button><p id="maintenance-message" class="meta"></p></details></section>
<dialog id="lightbox" aria-labelledby="lightbox-caption">
  <div class="lightbox-head"><div class="lightbox-controls"><button id="rotate-left" class="ghost" type="button" title="向左旋转" aria-label="向左旋转">↶</button><button id="zoom-out" class="ghost" type="button" title="缩小" aria-label="缩小">−</button><button id="zoom-reset" class="ghost" type="button" title="复位" aria-label="恢复原始比例">100%</button><button id="zoom-in" class="ghost" type="button" title="放大" aria-label="放大">＋</button><button id="rotate-right" class="ghost" type="button" title="向右旋转" aria-label="向右旋转">↷</button></div><button id="lightbox-close" class="ghost" type="button" aria-label="关闭图片预览">关闭</button></div>
  <div id="lightbox-stage" class="lightbox-stage"><img id="lightbox-image" class="lightbox-image" alt="" draggable="false"></div>
  <div class="lightbox-foot"><div class="lightbox-meta"><p id="lightbox-caption" class="lightbox-caption"></p><p id="lightbox-directory" class="lightbox-directory"></p></div><button id="lightbox-download" type="button">下载</button></div>
</dialog>
<div id="preferences-backdrop" class="modal-backdrop hidden"></div>
<section id="preferences" class="preferences hidden" role="dialog" aria-modal="true" aria-labelledby="preferences-title" tabindex="-1">
  <h2 id="preferences-title">显示设置</h2>
  <p class="meta">只影响当前浏览器。</p>
  <div class="pref-section">
    <h3>浏览</h3>
    <div class="preferences-grid">
      <label>视图<select id="layout-mode"><option value="slideshow">幻灯片</option><option value="waterfall">瀑布流</option></select></label>
      <label class="waterfall-only">手机瀑布流列数<select id="mobile-waterfall-columns"><option value="1">单列</option><option value="2">双列</option></select></label>
      <label>图片名称<select id="caption-mode"><option value="path">完整路径</option><option value="name">仅名称</option><option value="hidden">不展示</option></select></label>
      <label class="slideshow-only">自动播放（秒）<input id="slideshow-interval" type="number" min="0" max="300" step="1"></label>
    </div>
  </div>
  <div class="pref-section">
    <h3>画质</h3>
    <div class="preferences-grid">
      <label>列表预览<select id="preview-quality"><option value="176">极速 176</option><option value="480">清晰 480</option><option value="800">高清 800</option><option value="1280">超清 1280</option><option value="2560">极清 2560</option></select></label>
      <label>大图预览<select id="lightbox-quality"><option value="original">原图</option><option value="2560">极清 2560</option><option value="1280">超清 1280</option></select></label>
    </div>
  </div>
  <div class="pref-section">
    <h3>标签</h3>
    <label>筛选模式<select id="filter-mode"><option value="union">任一匹配</option><option value="intersect">全部匹配</option></select></label>
    <label class="check"><input id="show-tags-enabled" type="checkbox">在图片上显示标签</label>
  </div>
  <p class="meta">设置仅保存在当前浏览器。</p>
  <div class="preferences-actions"><button id="preferences-reset" class="ghost" type="button">恢复默认</button><button id="preferences-close" class="ghost" type="button">关闭</button><button id="preferences-save" type="button">保存</button></div>
</section>
<div id="announcement-backdrop" class="modal-backdrop announcement-backdrop hidden"></div>
<section id="announcement" class="announcement hidden" role="dialog" aria-modal="true" aria-labelledby="announcement-title">
  <div class="announcement-main"><h2 id="announcement-title" class="announcement-title"></h2><div id="announcement-content" class="announcement-content"></div></div>
  <div class="announcement-footer"><p id="announcement-reading" class="meta"></p><div class="announcement-actions"><button id="announcement-contact" class="hidden ghost" type="button">联系</button><button id="announcement-close-once" type="button">本次关闭</button><button id="announcement-close-forever" type="button">不再显示</button></div></div>
</section>
<button id="theme-fab" type="button" title="切换明暗主题" aria-label="切换明暗主题">🌙</button>
<script>
const PREFERENCE_KEY='openlist-image-preferences-v2';
const ANNOUNCEMENT_KEY_PREFIX='openlist-image-announcement-v2-';
const gallery=document.querySelector('#gallery');
const pageHeader=document.querySelector('header');
const statusEl=document.querySelector('#status');
const previousButton=document.querySelector('#previous');
const nextButton=document.querySelector('#next');
const slideshowToggle=document.querySelector('#slideshow-toggle');
const refreshButton=document.querySelector('#refresh');
const settingsButton=document.querySelector('#settings');
const announcementButton=document.querySelector('#announcement-button');
const contactWrap=document.querySelector('#contact-wrap');
const contactButton=document.querySelector('#contact-button');
const contactPopover=document.querySelector('#contact-popover');
const contactQr=document.querySelector('#contact-qr');
const menuContact=document.querySelector('#menu-contact');
const announcementContact=document.querySelector('#announcement-contact');
const maintenance=document.querySelector('#maintenance');
const maintenanceToken=document.querySelector('#maintenance-token');
const maintenanceMessage=document.querySelector('#maintenance-message');
const preferencesPanel=document.querySelector('#preferences');
const preferencesBackdrop=document.querySelector('#preferences-backdrop');
const layoutMode=document.querySelector('#layout-mode');
const mobileWaterfallColumns=document.querySelector('#mobile-waterfall-columns');
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
let lightboxReturnFocus=null;
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
let waterfallPrefetchPromise=null;
let waterfallPrefetchToken=0;
let renderGeneration=0;
let loadedCount=0;
let cardSequence=0;
let waterfallColumnCount=0;
let resizeTimer=null;
const slidePreloads=new Map();
const activePointers=new Map();
const urlResolveTasks=new Map();
const urlResolveQueue=[];
let urlResolveActive=0;
const URL_RESOLVE_CONCURRENCY=3;
const SLIDE_PRELOAD_COUNT=2;
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
  if(quality==='original') return image.url||sizedThumb(image.thumbnail,Number(settings&&settings.preview_quality)||176);
  return image.thumbnail?sizedThumb(image.thumbnail,quality==='1280'?1280:2560):image.url||'';
}

function normalizedPreferences(value,defaults){
  const stored=value&&typeof value==='object'?value:{};
  const defaultLayout=defaults.view_layout==='waterfall'?'waterfall':'slideshow';
  const storedLayout=['single','grid'].includes(stored.view_layout)?'slideshow':stored.view_layout;
  const result={view_layout:storedLayout,slideshow_interval:stored.slideshow_interval,grid_gap:stored.grid_gap,mobile_waterfall_columns:stored.mobile_waterfall_columns,caption_mode:stored.caption_mode,show_tags_enabled:stored.show_tags_enabled,theme:stored.theme,filter_mode:stored.filter_mode,preview_quality:stored.preview_quality,lightbox_quality:stored.lightbox_quality};
  if(!['slideshow','waterfall'].includes(result.view_layout)) result.view_layout=defaultLayout;
  if(!['path','name','hidden'].includes(result.caption_mode)) result.caption_mode=defaults.caption_mode;
  result.slideshow_interval=Math.max(0,Math.min(300,Number(stored.slideshow_interval??8)||0));
  result.grid_gap=Math.max(0,Math.min(48,Number(stored.grid_gap??defaults.grid_gap)||0));
  result.mobile_waterfall_columns=['1','2'].includes(String(result.mobile_waterfall_columns))?String(result.mobile_waterfall_columns):'1';
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
  preferences.contact=defaults.contact||{enabled:false,label:'联系',qq_number:'',qq_url:'',qr_url:''};
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
  return escapeHtml(value).replace(/&lt;font\s+color=(?:&quot;|&#39;)?(#[0-9a-f]{3,8}|[a-z]+)(?:&quot;|&#39;)?\s*&gt;/gi,'<span style="color:$1">').replace(/&lt;\/font&gt;/gi,'</span>').replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>').replace(/^#### (.*)$/gm,'<h4>$1</h4>').replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>').replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g,'<img src="$2" alt="$1" loading="lazy" referrerpolicy="no-referrer">').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>').replace(/\n\n/g,'</p><p>').replace(/\n/g,'<br>');
}

function todayKey(){const now=new Date();return now.getFullYear()+'-'+String(now.getMonth()+1).padStart(2,'0')+'-'+String(now.getDate()).padStart(2,'0');}

function announcementSeen(key){
  try{const value=localStorage.getItem(key);return value==='forever'||value==='day:'+todayKey();}catch(error){return false;}
}

function closeAnnouncement(key,persist){
  if(announcementTimer) clearInterval(announcementTimer);
  try{localStorage.setItem(key,persist?'forever':'day:'+todayKey());}catch(error){}
  announcementPanel.classList.add('is-closing');
  setTimeout(()=>{announcementPanel.classList.add('hidden');announcementPanel.classList.remove('is-closing');announcementBackdrop.classList.add('hidden');document.body.classList.remove('announcement-open');if(announcementReturnFocus&&announcementReturnFocus.focus)announcementReturnFocus.focus();scheduleSlideshow();},220);
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

let announcementReturnFocus=null;
function isMobileContact(){return /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);}
function contactConfig(){return settings&&settings.contact&&settings.contact.enabled?settings.contact:null;}
function contactHref(){
  const contact=contactConfig();
  if(!contact) return '';
  if(isMobileContact()){
    if(contact.qq_number) return 'mqqwpa://im/chat?chat_type=wpa&uin='+encodeURIComponent(contact.qq_number)+'&version=1&src_type=web&web_src=oicqzone.com';
    return contact.qq_url||'';
  }
  if(contact.qq_url) return contact.qq_url;
  if(contact.qq_number) return 'tencent://message/?uin='+encodeURIComponent(contact.qq_number)+'&Site=OpenList&Menu=yes';
  return '';
}
function syncContactControls(){
  const contact=contactConfig();
  const enabled=!!contact;
  const label=(contact&&contact.label)||'联系';
  if(contactWrap) contactWrap.classList.toggle('hidden',!enabled);
  if(contactButton) contactButton.textContent=label;
  if(menuContact){menuContact.classList.toggle('hidden',!enabled);menuContact.textContent=label;}
  if(announcementContact){announcementContact.classList.toggle('hidden',!enabled);announcementContact.textContent=label;}
  if(contactQr){
    if(contact&&contact.qr_url){contactQr.src=contact.qr_url;contactQr.alt=label+' 二维码';}
    else {contactQr.removeAttribute('src');contactQr.alt='';}
  }
}
function hideContactPopover(){if(contactPopover) contactPopover.classList.add('hidden');}
function showContactPopover(){if(!contactConfig()||!contactConfig().qr_url||!contactPopover) return;contactPopover.classList.remove('hidden');}
function openContact(){
  const href=contactHref();
  if(!href) return;
  hideContactPopover();
  window.open(href,'_blank');
}

function showAnnouncement(force=false){
  const announcement=settings.announcement;
  if(!announcement||!announcement.enabled||!announcement.content.trim()) return;
  const key=ANNOUNCEMENT_KEY_PREFIX+announcement.version;
  if(!force&&announcementSeen(key)) return;
  clearSlideTimer();
  announcementReturnFocus=document.activeElement;
  announcementTitle.textContent=announcement.title||'网站公告';
  announcementContent.innerHTML='<p>'+renderMarkdown(announcement.content)+'</p>';
  document.body.classList.add('announcement-open');
  announcementPanel.classList.remove('hidden');
  announcementPanel.classList.remove('is-closing');
  announcementBackdrop.classList.remove('hidden');
  setAnnouncementCountdown(force?0:announcement.required_seconds,key);
  const first=focusableWithin(announcementPanel)[0];
  if(first) first.focus();
}

function persistPreferences(){try{localStorage.setItem(PREFERENCE_KEY,JSON.stringify({view_layout:settings.view_layout,slideshow_interval:settings.slideshow_interval,grid_gap:settings.grid_gap,mobile_waterfall_columns:settings.mobile_waterfall_columns,caption_mode:settings.caption_mode,show_tags_enabled:settings.show_tags_enabled,theme:settings.theme,filter_mode:settings.filter_mode,preview_quality:settings.preview_quality,lightbox_quality:settings.lightbox_quality}));}catch(error){statusEl.textContent='设置无法保存到当前浏览器';}}

let preferencesReturnFocus=null;
function focusableWithin(root){return [...root.querySelectorAll('button:not([disabled]),a[href],input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')].filter(element=>!element.closest('.hidden'));}
function trapFocus(root,event){
  if(event.key!=='Tab') return;
  const items=focusableWithin(root);
  if(!items.length) return;
  const first=items[0],last=items[items.length-1];
  if(event.shiftKey&&document.activeElement===first){event.preventDefault();last.focus();}
  else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first.focus();}
}
function openPreferences(){
  clearSlideTimer();
  preferencesReturnFocus=document.activeElement;
  previewQuality.value=settings.preview_quality;
  lightboxQuality.value=settings.lightbox_quality;
  layoutMode.value=settings.view_layout;
  slideshowInterval.value=settings.slideshow_interval;
  syncSlideshowOption();
  captionMode.value=settings.caption_mode;
  filterMode.value=settings.filter_mode;
  showTagsEnabled.checked=settings.show_tags_enabled;
  mobileWaterfallColumns.value=settings.mobile_waterfall_columns;
  syncWaterfallOption();
  preferencesPanel.classList.remove('hidden');
  preferencesBackdrop.classList.remove('hidden');
  preferencesPanel.focus();
}

function closePreferences(){preferencesPanel.classList.add('hidden');preferencesBackdrop.classList.add('hidden');if(preferencesReturnFocus&&preferencesReturnFocus.focus)preferencesReturnFocus.focus();preferencesReturnFocus=null;scheduleSlideshow();}

function syncSlideshowOption(){document.querySelector('.slideshow-only').classList.toggle('hidden',layoutMode.value!=='slideshow');}
function syncWaterfallOption(){document.querySelector('.waterfall-only').classList.toggle('hidden',layoutMode.value!=='waterfall');}
layoutMode.addEventListener('change',()=>{syncSlideshowOption();syncWaterfallOption();});

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
    const externalSignal=options.signal;
    const abort=()=>controller.abort();
    if(externalSignal){
      if(externalSignal.aborted) throw Object.assign(new Error('请求已取消'),{name:'AbortError'});
      externalSignal.addEventListener('abort',abort,{once:true});
    }
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
      const cancelled=Boolean(externalSignal&&externalSignal.aborted);
      const retryable=!cancelled&&(error.name!=='AbortError'||!externalSignal||!externalSignal.aborted)&&error.retryable!==false;
      if(!retryable||attempt===attempts-1) break;
      await wait(500*Math.pow(2,attempt));
    }finally{
      clearTimeout(timeout);
      if(externalSignal) externalSignal.removeEventListener('abort',abort);
    }
  }
  throw lastError||new Error('请求失败');
}

function applyResolvedUrl(image,data){
  if(!data||data.error) return image;
  const received=Boolean(data.url||data.thumbnail);
  if(data.url) image.url=data.url;
  if(data.thumbnail) image.thumbnail=data.thumbnail;
  if(received){
    image._resolvedAt=Date.now();
    image.needs_url=false;
  }
  return image;
}

function needsImageResolve(image,force=false,preview=true){
  const sourceReady=preview?!!image.thumbnail:!!image.url;
  return !!(image&&image.path&&(force||!sourceReady||Date.now()-(image._resolvedAt||0)>URL_REFRESH_AGE_MS));
}

function cancelUrlResolveTasks(){
  urlResolveQueue.splice(0,urlResolveQueue.length).forEach(task=>{
    task.cancelled=true;
    task.reject(Object.assign(new Error('请求已取消'),{name:'AbortError'}));
  });
  urlResolveTasks.forEach(task=>{
    task.cancelled=true;
    task.controller&&task.controller.abort();
  });
  urlResolveTasks.clear();
}

function runNextUrlResolve(){
  while(urlResolveActive<URL_RESOLVE_CONCURRENCY&&urlResolveQueue.length){
    urlResolveQueue.sort((left,right)=>left.priority-right.priority||left.sequence-right.sequence);
    const task=urlResolveQueue.shift();
    if(task.cancelled) continue;
    urlResolveActive+=1;
    task.started=true;
    task.run().then(task.resolve,task.reject).finally(()=>{
      urlResolveActive-=1;
      if(urlResolveTasks.get(task.key)===task) urlResolveTasks.delete(task.key);
      runNextUrlResolve();
    });
  }
}

let urlResolveSequence=0;
function queueImageResolve(image,{force=false,preview=true,priority=2}={}){
  if(!needsImageResolve(image,force,preview)) return Promise.resolve(image);
  const key=image.path+'|'+(preview?'preview':'download');
  const existing=urlResolveTasks.get(key);
  if(existing){
    existing.priority=Math.min(existing.priority,priority);
    existing.force=existing.force||force;
    return existing.promise.then(data=>applyResolvedUrl(image,data));
  }
  const task={key,priority,force,sequence:urlResolveSequence++,controller:new AbortController(),started:false,cancelled:false};
  task.promise=new Promise((resolve,reject)=>{task.resolve=resolve;task.reject=reject;});
  task.run=async()=>{
    const fresh=task.force?'&fresh=1':'';
    const mode=preview?'&preview=1':'';
    return fetchJsonWithRetry('/api/download-url?path='+encodeURIComponent(image.path)+fresh+mode,{headers:adminHeaders(),signal:task.controller.signal},2);
  };
  urlResolveTasks.set(key,task);
  urlResolveQueue.push(task);
  runNextUrlResolve();
  return task.promise.then(data=>applyResolvedUrl(image,data));
}

async function refreshImageUrls(images,force=false,preview=true,priority=2){
  await Promise.all(images.map(image=>queueImageResolve(image,{force,preview,priority}).catch(()=>image)));
  return images;
}

function refreshImageUrl(image,force=true,preview=true,priority=0){
  return queueImageResolve(image,{force,preview,priority});
}

async function ensureFreshImage(image,preview=true,priority=2){
  if(needsImageResolve(image,false,preview)) await queueImageResolve(image,{force:false,preview,priority});
  return image;
}

function attachImageRecovery(element,image){
  element.addEventListener('load',()=>{delete element.dataset.refreshAttempted;},{once:false});
  element.addEventListener('error',()=>{
    if(element.dataset.refreshAttempted==='1') return;
    element.dataset.refreshAttempted='1';
    refreshImageUrl(image,true,true).then(()=>{element.src=cardSrc(image);}).catch(()=>{});
  });
}

async function downloadImage(){
  if(!activeImage) return;
  if(!activeImage.url||Date.now()-(activeImage._resolvedAt||0)>URL_REFRESH_AGE_MS){
    await refreshImageUrl(activeImage,false,false);
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

let lightboxResolveToken=0;
function openLightbox(image){
  clearSlideTimer();
  lightboxReturnFocus=document.activeElement;
  const token=++lightboxResolveToken;
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
    refreshImageUrl(image,true,false,0).then(()=>{if(token===lightboxResolveToken)lightboxImage.src=lightboxSrc(image);}).catch(()=>{});
  };
  lightboxImage.alt=imageName(image.path)||'图片';
  const previewSrc=lightboxSrc(image);
  if(previewSrc) lightboxImage.src=previewSrc;
  const wantOriginal=(settings.lightbox_quality||'original')==='original';
  if(wantOriginal||!previewSrc){
    refreshImageUrl(image,false,false,0).then(()=>{
      if(token!==lightboxResolveToken||!lightbox.open)return;
      const next=lightboxSrc(image);
      if(next&&lightboxImage.src!==next) lightboxImage.src=next;
    }).catch(()=>{});
  }
  if(lightboxImage.complete&&lightboxImage.naturalWidth) fitLightboxImage(false);
}

function createCard(image,eager=false){
  const card=document.createElement('article');
  card.className='card is-loading';
  card.dataset.sequence=String(cardSequence++);
  card.dataset.path=image.path||'';
  const preview=document.createElement('button');
  preview.className='preview-button';
  preview.type='button';
  const accessibleName=imageName(image.path)||'图片';
  preview.setAttribute('aria-label','查看图片：'+accessibleName);
  preview.onclick=()=>openLightbox(image);
  const picture=document.createElement('img');
  picture.loading=eager?'eager':'lazy';
  picture.decoding='async';
  if(eager) picture.fetchPriority='high';
  picture.alt='';
  attachImageRecovery(picture,image);
  const markCardReady=()=>{card.classList.remove('is-loading');picture.alt=accessibleName;};
  picture.addEventListener('load',markCardReady);
  const showCardError=()=>{
    card.classList.remove('is-loading');
    picture.classList.add('hidden');
    let error=card.querySelector('.image-error');
    if(!error){
      error=document.createElement('div');
      error.className='image-error';
      const message=document.createElement('span');
      message.textContent='图片加载失败';
      const retry=document.createElement('button');
      retry.className='ghost';
      retry.type='button';
      retry.textContent='重试';
      retry.onclick=event=>{event.stopPropagation();error.remove();picture.classList.remove('hidden');card.classList.add('is-loading');picture.alt='';delete picture.dataset.srcApplied;queueImageResolve(image,{force:true,preview:true,priority:0}).then(()=>{picture.src=cardSrc(image);}).catch(showCardError);};
      error.append(message,retry);
      card.append(error);
    }
  };
  const applySrc=()=>{
    if(picture.dataset.srcApplied==='1') return card._ready||Promise.resolve();
    picture.dataset.srcApplied='1';
    if(needsImageResolve(image,false,true)){
      card._ready=ensureFreshImage(image,true,eager?0:2).then(()=>{const src=cardSrc(image);if(!src)throw new Error('missing image source');picture.src=src;}).catch(showCardError);
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
    like.setAttribute('aria-label','喜欢 '+accessibleName);
    like.setAttribute('aria-pressed','false');
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
        const active=image.tags.categories&&image.tags.categories.includes(cat);
        btn.classList.toggle('active',!!active);
        btn.setAttribute('aria-pressed',String(!!active));
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
      trash.setAttribute('aria-label','标记为垃圾图片：'+accessibleName);
      trash.dataset.category=taggingConfig.trash_tag;
      const active=image.tags.categories&&image.tags.categories.includes(taggingConfig.trash_tag);
      trash.classList.toggle('active',!!active);
      trash.setAttribute('aria-pressed',String(!!active));
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
  const images=(data.images||[]).map(image=>({...image,_resolvedAt:image.url||image.thumbnail?Date.now():0}));
  const pending=images.filter(image=>needsImageResolve(image,false,true));
  if(!pending.length) return images;
  try{
    const resolved=await fetchJsonWithRetry('/api/download-url',{
      method:'POST',
      headers:{'Content-Type':'application/json',...adminHeaders()},
      body:JSON.stringify({paths:pending.map(image=>image.path),preview:true})
    },2);
    const resolvedByPath=new Map((resolved.images||[]).map(image=>[image.path,image]));
    pending.forEach(image=>applyResolvedUrl(image,resolvedByPath.get(image.path)));
  }catch(error){
    if(error.name!=='AbortError') console.debug('批量预览解析失败，将按图片重试',error);
  }
  return images;
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
    button.setAttribute('aria-pressed',String(newValue));
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
    button.setAttribute('aria-pressed',String(newValue));
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
    const active=activeTagFilters.includes(cat);
    chip.classList.toggle('active',active);
    chip.setAttribute('aria-pressed',String(active));
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
    chip.setAttribute('aria-label',taggingConfig.trash_tag);
    const count=tagCategoriesCache.categories&&tagCategoriesCache.categories[taggingConfig.trash_tag];
    if(count){const c=document.createElement('span');c.className='tag-chip-count';c.textContent='('+count+')';chip.append(c);}
    const active=activeTagFilters.includes(taggingConfig.trash_tag);
    chip.classList.toggle('active',active);
    chip.setAttribute('aria-pressed',String(active));
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
  if(width<=560) return Number(settings&&settings.mobile_waterfall_columns)||1;
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
  if(navPause){navPause.textContent=slideshowPaused?'播放':'暂停';navPause.setAttribute('aria-label',slideshowPaused?'继续播放':'暂停播放');}
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
  const urls=new Set(upcoming.map(image=>cardSrc(image)).filter(Boolean));
  for(const [url,image] of slidePreloads){if(!urls.has(url)){image.src='';slidePreloads.delete(url);}}
  for(const image of upcoming){
    const src=cardSrc(image);
    if(!src||slidePreloads.has(src)) continue;
    const preload=new Image();
    preload.decoding='async';
    preload.src=src;
    slidePreloads.set(src,preload);
  }
}

async function appendSlideImages(count,generation=renderGeneration){
  if(slideExhausted) return [];
  if(slideLoadPromise) return slideLoadPromise;
  const loadPromise=requestImages(count).then(images=>{
    if(generation!==renderGeneration) return [];
    slideImages.push(...images);
    if(images.length<count) slideExhausted=true;
    return images;
  }).catch(error=>{
    if(generation===renderGeneration) slideExhausted=true;
    throw error;
  }).finally(()=>{if(slideLoadPromise===loadPromise)slideLoadPromise=null;});
  slideLoadPromise=loadPromise;
  return loadPromise;
}

async function ensureSlideBuffer(generation=renderGeneration){
  const remaining=slideImages.length-slideIndex-1;
  if(remaining<SLIDE_PRELOAD_COUNT&&!slideExhausted) await appendSlideImages(SLIDE_PRELOAD_COUNT-remaining,generation);
  if(generation!==renderGeneration) return;
  const current=slideImages[slideIndex];
  const upcoming=slideImages.slice(slideIndex+1,slideIndex+1+SLIDE_PRELOAD_COUNT);
  if(current) queueImageResolve(current,{priority:0}).then(()=>{if(generation===renderGeneration&&slideImages[slideIndex]===current)renderSlideshow();}).catch(()=>{});
  refreshImageUrls(upcoming,false,true,1).then(()=>{if(generation===renderGeneration)preloadUpcomingSlides();}).catch(()=>{});
}

function recordSlideHistory(image){
  if(!image._historyId) image._historyId=++slideHistorySequence;
  if(slideHistory.some(item=>item._historyId===image._historyId)) return;
  slideHistory.push(image);
  if(slideHistory.length>60) slideHistory.splice(0,slideHistory.length-60);
}

async function showHistoryImage(image){
  setSlideshowPaused(true);
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

async function loadSlideshow(reset,generation=renderGeneration){
  clearSlideTimer();
  if(reset){
    slideImages=[];
    slideIndex=0;
    slideExhausted=false;
    slideHistory=[];
    slideHistorySequence=0;
    slidePreloads.clear();
    await appendSlideImages(SLIDE_INITIAL_LOAD,generation);
  }
  if(generation!==renderGeneration) return;
  await ensureSlideBuffer(generation);
  if(generation===renderGeneration) renderSlideshow();
}

async function nextSlide(){
  clearSlideTimer();
  await ensureSlideBuffer();
  if(slideIndex<slideImages.length-1){
    slideIndex+=1;
  }else if(slideExhausted){
    setSlideshowPaused(true);
  }
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
  waterfallPrefetchPromise=null;
  waterfallPrefetchToken+=1;
}

function prefetchNextWaterfallBatch(){
  if(waterfallPrefetchPromise||waterfallExhausted||waterfallPrefetch.length) return waterfallPrefetchPromise;
  const token=waterfallPrefetchToken;
  waterfallPrefetchPromise=requestImages(WATERFALL_BATCH_SIZE).then(images=>{
    if(token!==waterfallPrefetchToken) return [];
    if(!images.length){waterfallExhausted=true;return [];}
    waterfallPrefetch=images;
    return images;
  }).catch(()=>{
    if(token===waterfallPrefetchToken) waterfallPrefetch=[];
    return [];
  }).finally(()=>{
    if(token===waterfallPrefetchToken) waterfallPrefetchPromise=null;
  });
  return waterfallPrefetchPromise;
}

async function loadWaterfallBatch(reset,generation=renderGeneration){
  if(waterfallLoading) return;
  if(!reset&&waterfallExhausted) return;
  waterfallLoading=true;
  refreshButton.disabled=true;
  gallery.setAttribute('aria-busy','true');
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
    if(generation!==renderGeneration) return;
    if(!waterfallPrefetch.length&&waterfallPrefetchPromise) await waterfallPrefetchPromise;
    let images=waterfallPrefetch.length?waterfallPrefetch.splice(0,waterfallPrefetch.length):await requestImages(WATERFALL_BATCH_SIZE);
    if(generation!==renderGeneration) return;
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
      prefetchNextWaterfallBatch();
    }
  }catch(error){
    if(generation!==renderGeneration) return;
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
    if(generation===renderGeneration){
      waterfallLoading=false;
      refreshButton.disabled=false;
      gallery.setAttribute('aria-busy','false');
      maybeLoadMoreWaterfall();
    }
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
  cancelUrlResolveTasks();
  lightboxResolveToken+=1;
  const generation=++renderGeneration;
  slideLoadPromise=null;
  waterfallLoading=false;
  gallery.setAttribute('aria-busy','true');
  refreshButton.disabled=true;
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
  if(restricted){statusEl.textContent='维护中';gallery.setAttribute('aria-busy','false');return;}
  statusEl.textContent='正在加载…';
  applyGridStyle();
  try{
    if(settings.view_layout==='slideshow'){
      await loadSlideshow(true,generation);
    }else{
      gallery.className='gallery waterfall';
      await loadWaterfallBatch(true,generation);
    }
  }finally{
    if(generation===renderGeneration){gallery.setAttribute('aria-busy','false');refreshButton.disabled=false;}
  }
}

function showError(error){
  if(error&&error.name==='AbortError') return;
  statusEl.textContent='加载失败：'+error.message;
  refreshButton.disabled=false;
  gallery.setAttribute('aria-busy','false');
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
if(contactButton){
  contactButton.onclick=event=>{event.preventDefault();openContact();};
  contactButton.addEventListener('mouseenter',()=>{if(!isMobileContact()) showContactPopover();});
  contactButton.addEventListener('mouseleave',()=>{if(!isMobileContact()) hideContactPopover();});
  if(contactWrap) contactWrap.addEventListener('mouseleave',()=>{if(!isMobileContact()) hideContactPopover();});
  let contactPressTimer=null;
  const clearContactPress=()=>{if(contactPressTimer){clearTimeout(contactPressTimer);contactPressTimer=null;}};
  contactButton.addEventListener('touchstart',()=>{if(!isMobileContact()) return;clearContactPress();contactPressTimer=setTimeout(()=>showContactPopover(),420);},{passive:true});
  contactButton.addEventListener('touchend',clearContactPress,{passive:true});
  contactButton.addEventListener('touchcancel',clearContactPress,{passive:true});
  contactButton.addEventListener('touchmove',clearContactPress,{passive:true});
}
if(announcementContact) announcementContact.onclick=event=>{event.preventDefault();openContact();};
const headerMenuToggle=document.querySelector('#header-menu-toggle');
const headerMenu=document.querySelector('#header-menu');
const headerMenuBackdrop=document.querySelector('#header-menu-backdrop');
function openHeaderMenu(){if(headerMenu.classList.contains('open')){closeHeaderMenu();return;}clearSlideTimer();headerMenuReturnFocus=document.activeElement;headerMenu.style.setProperty('--menu-top',Math.round(pageHeader.getBoundingClientRect().bottom)+'px');headerMenu.classList.add('open');headerMenuBackdrop.classList.add('open');headerMenuToggle.setAttribute('aria-expanded','true');const first=focusableWithin(headerMenu)[0];if(first)first.focus();}
let headerMenuReturnFocus=null;
function closeHeaderMenu(returnFocus=false){headerMenu.classList.remove('open');headerMenuBackdrop.classList.remove('open');headerMenuToggle.setAttribute('aria-expanded','false');if(returnFocus&&headerMenuReturnFocus&&headerMenuReturnFocus.focus)headerMenuReturnFocus.focus();headerMenuReturnFocus=null;scheduleSlideshow();}
headerMenuToggle.onclick=openHeaderMenu;
headerMenuBackdrop.onclick=closeHeaderMenu;
document.querySelector('#menu-previous').onclick=()=>{closeHeaderMenu();previousSlide();};
document.querySelector('#menu-next').onclick=()=>{closeHeaderMenu();nextSlide().catch(showError);};
document.querySelector('#menu-slideshow-toggle').onclick=()=>{closeHeaderMenu();setSlideshowPaused(!slideshowPaused);};
document.querySelector('#menu-refresh').onclick=()=>{closeHeaderMenu();render().catch(showError);};
document.querySelector('#menu-settings').onclick=()=>{closeHeaderMenu();openPreferences();};
document.querySelector('#menu-announcement').onclick=()=>{closeHeaderMenu();showAnnouncement(true);};
if(menuContact) menuContact.onclick=()=>{closeHeaderMenu();openContact();};
document.querySelector('#maintenance-unlock').onclick=async()=>{const token=maintenanceToken.value.trim();if(!token){maintenanceMessage.textContent='请输入管理密钥。';return;}maintenanceMessage.textContent='正在验证…';const response=await fetch('/api/admin/config',{headers:{'X-OpenList-Admin-Token':token},cache:'no-store'});if(!response.ok){maintenanceMessage.textContent='管理密钥无效。';return;}maintenanceAccessToken=token;maintenanceMessage.textContent='';render().catch(showError);};
lightboxDownload.onclick=()=>downloadImage().catch(showError);
document.querySelector('#preferences-save').onclick=()=>{settings.view_layout=layoutMode.value;settings.slideshow_interval=Math.max(0,Math.min(300,Number(slideshowInterval.value)||0));settings.mobile_waterfall_columns=['1','2'].includes(mobileWaterfallColumns.value)?mobileWaterfallColumns.value:'1';settings.caption_mode=captionMode.value;settings.show_tags_enabled=showTagsEnabled.checked;settings.filter_mode=filterMode.value;settings.preview_quality=previewQuality.value;settings.lightbox_quality=lightboxQuality.value;persistPreferences();location.reload();};
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
lightbox.addEventListener('close',()=>{activePointers.clear();dragStart=null;pinchStart=null;lightboxResolveToken+=1;lightboxStage.classList.remove('is-dragging');if(lightboxReturnFocus&&lightboxReturnFocus.focus)lightboxReturnFocus.focus();lightboxReturnFocus=null;scheduleSlideshow();});
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
  if(event.key==='Escape'&&!preferencesPanel.classList.contains('hidden')){closePreferences();return;}
  if(event.key==='Escape'&&headerMenu.classList.contains('open')){closeHeaderMenu(true);return;}
  if(!preferencesPanel.classList.contains('hidden')){trapFocus(preferencesPanel,event);return;}
  if(headerMenu.classList.contains('open')){if(event.key==='Escape')closeHeaderMenu(true);else trapFocus(headerMenu,event);return;}
  if(!lightbox.open) return;
  if(event.key==='Escape') lightbox.close();
  else if(event.key==='ArrowLeft'){event.preventDefault();previousSlide();}
  else if(event.key==='ArrowRight'){event.preventDefault();nextSlide().catch(showError);}
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
    if(pageHeader)document.documentElement.style.setProperty('--header-h',Math.ceil(pageHeader.getBoundingClientRect().height)+'px');
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
if(pageHeader)document.documentElement.style.setProperty('--header-h',Math.ceil(pageHeader.getBoundingClientRect().height)+'px');
loadSettings().then(value=>{settings=value;taggingConfig=settings.tagging;applyGalleryTheme(settings.theme);const annVisible=settings.announcement.enabled;announcementButton.classList.toggle('hidden',!annVisible);document.querySelector('#menu-announcement').classList.toggle('hidden',!annVisible);syncContactControls();showAnnouncement();loadTagCategories();return render();}).catch(showError);
</script>
</body>
</html>"""


def admin_html() -> str:
    return r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>图库管理</title>
<style>
:root{
  color-scheme:dark;
  --bg:#07080c;--bg-elev:#0e1218;--bg-soft:#141922;--line:#1f2633;--text:#d7deea;--muted:#7b879b;--accent:#3d8bfd;--accent-hover:#5aa0ff;--danger:#ff5d6c;--radius:14px;--shadow:0 16px 40px rgba(0,0,0,.5);
  --font:"Segoe UI",system-ui,-apple-system,"PingFang SC","Noto Sans SC",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 var(--font)}
.wrap{max-width:980px;margin:0 auto;padding:22px 18px 80px}
.admin-head{display:flex;align-items:flex-end;justify-content:space-between;gap:12px;margin-bottom:18px}
.admin-head h1{margin:0;font-size:24px}
.admin-head a{color:var(--accent);text-decoration:none}
section{margin-bottom:16px;padding:18px;background:var(--bg-elev);border:1px solid var(--line);border-radius:var(--radius)}
h2,h3{margin:0 0 10px}
h2{font-size:18px}h3{font-size:14px;color:var(--muted)}
label{display:grid;gap:6px;margin:10px 0}
.row{display:flex;gap:12px;flex-wrap:wrap}
.row>label{flex:1;min-width:180px}
input,select,textarea,button{font:inherit}
input,select,textarea{width:100%;padding:9px 10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text)}
input:focus-visible,select:focus-visible,textarea:focus-visible,button:focus-visible,a:focus-visible{outline:3px solid color-mix(in srgb,var(--accent) 55%,transparent);outline-offset:2px;border-color:var(--accent)}
textarea{min-height:80px;resize:vertical}
button{padding:9px 16px;border:0;border-radius:10px;background:var(--accent);color:#fff;cursor:pointer}
button:hover{background:var(--accent-hover)}
button:disabled{opacity:.5;cursor:not-allowed}
button.secondary,button.ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
button.danger{background:transparent;color:var(--danger);border:1px solid color-mix(in srgb,var(--danger) 45%,var(--line))}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.note{color:var(--muted);font-size:13px}
.selected{display:grid;gap:6px;margin-top:10px}
.selected-item{display:flex;gap:8px;align-items:center;justify-content:space-between;padding:8px 10px;border:1px solid var(--line);border-radius:10px;background:var(--bg)}
.check{display:flex;align-items:center;gap:8px}
.check input{width:auto}
.markdown-preview{min-height:80px;padding:12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);line-height:1.65}
.markdown-preview img{display:block;max-width:100%;height:auto;margin:12px auto;border-radius:10px}
.hidden{display:none}
.trash-list{display:flex;flex-direction:column;gap:8px;margin-top:10px;max-height:520px;overflow-y:auto}
.trash-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.trash-item input[type=checkbox]{width:auto;flex:0 0 auto}
.trash-thumb{width:72px;height:72px;flex:0 0 auto;object-fit:cover;border-radius:8px;background:#05060a;display:block}
.trash-item .trash-path{flex:1;min-width:0;word-break:break-all;font-size:13px}
a{color:#b7d1ff}
.tabs-nav{display:flex;gap:6px;overflow-x:auto;padding-bottom:2px;margin:-4px 0 16px;border-bottom:1px solid var(--line);scrollbar-width:thin}
.tab-button{padding:10px 14px;background:transparent;color:var(--muted);border:0;border-radius:10px 10px 0 0;white-space:nowrap}
.tab-button.active{color:var(--text);background:color-mix(in srgb,var(--accent) 16%,transparent);box-shadow:inset 0 -2px 0 var(--accent)}
.tab-panel{display:none}
.tab-panel.active{display:block}
.tree{max-height:440px;overflow:auto;border:1px solid var(--line);border-radius:10px;padding:8px;background:var(--bg)}
.tree-node{margin:2px 0}
.tree-row{display:flex;align-items:center;gap:6px;padding:6px;border-radius:8px}
.tree-row:hover{background:color-mix(in srgb,var(--accent) 10%,transparent)}
.tree-toggle{width:24px;height:24px;padding:0;text-align:center;font-size:12px;color:var(--accent);background:transparent;border:0}
.tree-check{width:auto}
.tree-label{flex:1;word-break:break-all}
.tree-children{margin-left:22px;display:none}
.tree-node.open>.tree-children{display:block}
.theme-toggle{position:fixed;bottom:max(18px,env(safe-area-inset-bottom));right:max(18px,env(safe-area-inset-right));z-index:60;width:48px;height:48px;padding:0;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:22px;box-shadow:var(--shadow)}
.sticky-save{position:sticky;bottom:0;padding-top:14px;margin-top:18px;background:linear-gradient(transparent,var(--bg) 28%)}
body.theme-light{color-scheme:light;--bg:#f3f5f8;--bg-elev:#fff;--bg-soft:#eef1f6;--line:#d5dbe6;--text:#1a2230;--muted:#667085;--accent:#2d6cf0;--accent-hover:#1f5ee0;--shadow:0 14px 32px rgba(30,40,60,.12)}
body.theme-light a{color:#2d6cf0}
body:not(.theme-light)::after{content:'';position:fixed;inset:0;z-index:10000;pointer-events:none;background:rgba(0,0,0,.32)}
@media(max-width:640px){
  .wrap{padding:14px 12px 88px}
  .admin-head{align-items:flex-start;flex-direction:column}
  .row.actions button,.actions button{flex:1 1 calc(50% - 8px)}
  .tabs-nav{margin-left:-4px;margin-right:-4px}
}
</style>
</head>
<body>
<button id="theme-toggle" class="theme-toggle secondary" type="button" title="切换明暗主题" aria-label="切换明暗主题" aria-pressed="false">🌙</button>
<div class="wrap">
<header class="admin-head">
  <div><h1>图库管理</h1><p class="note" style="margin:6px 0 0">改这里会影响所有访客。</p></div>
  <a href="/gallery">返回浏览</a>
</header>
<section>
  <h2>登录</h2>
  <label>管理令牌<input id="token" type="password" autocomplete="current-password"></label>
  <div class="actions"><button id="load" type="button">加载配置</button></div>
  <p class="note hidden" id="admin-status" aria-live="polite"></p>
</section>
<section id="protected" class="hidden">
  <div class="tabs-nav" role="tablist" aria-label="管理设置">
    <button id="tab-button-directories" class="tab-button active" data-tab="directories" type="button" role="tab" aria-selected="true" aria-controls="tab-directories" tabindex="0">目录</button>
    <button id="tab-button-display" class="tab-button" data-tab="display" type="button" role="tab" aria-selected="false" aria-controls="tab-display" tabindex="-1">显示</button>
    <button id="tab-button-announcement" class="tab-button" data-tab="announcement" type="button" role="tab" aria-selected="false" aria-controls="tab-announcement" tabindex="-1">公告</button>
    <button id="tab-button-tags" class="tab-button" data-tab="tags" type="button" role="tab" aria-selected="false" aria-controls="tab-tags" tabindex="-1">标签</button>
    <button id="tab-button-tools" class="tab-button" data-tab="tools" type="button" role="tab" aria-selected="false" aria-controls="tab-tools" tabindex="-1">维护</button>
  </div>
  <p class="note">多名管理员同时保存时，以最后一次为准。</p>

  <div id="tab-directories" class="tab-panel active" role="tabpanel" aria-labelledby="tab-button-directories">
    <h2>浏览 OpenList 目录</h2>
    <div class="row">
      <label style="flex:2">当前路径<input id="path" value="/"></label>
    </div>
    <div class="actions"><button id="browse" type="button">加载目录树</button></div>
    <p class="note">目录树实时读取自 OpenList，总是最新状态，无需刷新缓存。勾选即可加入已选列表；点目录名展开子目录。</p>
    <div id="directories" class="tree"><p class="note">点击“加载目录树”开始浏览。</p></div>
    <h3>已选目录</h3>
    <div id="selected" class="selected"></div>
  </div>

  <div id="tab-display" class="tab-panel" role="tabpanel" aria-labelledby="tab-button-display">
    <h2>显示与主题</h2>
    <label>新访客的图片文字<select id="default-caption"><option value="path">完整路径</option><option value="name">仅图片名称</option><option value="hidden">不展示</option></select></label>
    <label class="check"><input id="directory-display-enabled" type="checkbox">完整路径模式下显示目录</label>
    <label>隐藏前 N 层目录<input id="directory-display-depth" type="number" min="0" max="64" step="1"></label>
    <p class="note">路径 1/2/3/4，隐藏 1 层后显示 2/3/4。</p>
    <label>默认主题<select id="theme"><option value="dark">暗色</option><option value="light">浅色</option></select></label>
    <p class="note">访客仍可用右下角按钮临时切换。</p>
  </div>

  <div id="tab-announcement" class="tab-panel" role="tabpanel" aria-labelledby="tab-button-announcement">
    <h2>网站公告</h2>
    <label class="check"><input id="announcement-enabled" type="checkbox">启用公告弹窗</label>
    <label>公告标题<input id="announcement-title" maxlength="120"></label>
    <label>公告内容（Markdown）<textarea id="announcement-content" maxlength="4000" placeholder="# 标题&#10;支持 **加粗**、*斜体*、`代码`、链接、图片和代码块&#10;![说明](https://example.com/notice.png)"></textarea></label>
    <p class="note">图片请使用公网 http/https 地址，例如 ![说明](https://example.com/notice.png)。不支持 OpenList 签名链接。</p>
    <div class="actions"><button id="announcement-preview-button" class="secondary" type="button">预览</button></div>
    <div id="announcement-preview" class="markdown-preview"></div>
    <label>强制阅读秒数<input id="announcement-required-seconds" type="number" min="0" max="3600" step="1"></label>
    <p class="note">修改标题、内容、开关或秒数都会生成新公告版本。</p>
    <h3>联系方式</h3>
    <label class="check"><input id="contact-enabled" type="checkbox">显示联系按钮</label>
    <label>按钮文字<input id="contact-label" maxlength="20" placeholder="联系"></label>
    <label>QQ 号码<input id="contact-qq-number" inputmode="numeric" maxlength="12" placeholder="3473905540"></label>
    <label>电脑端加好友链接<input id="contact-qq-url" maxlength="300" placeholder="https://qm.qq.com/q/..."></label>
    <label>二维码图片地址<input id="contact-qr-url" maxlength="300" placeholder="https://example.com/qq-qr.png"></label>
    <p class="note">电脑悬停显示二维码，点击打开 QQ；手机长按显示二维码，点击跳转加好友。二维码需使用公网图片地址。</p>
  </div>

  <div id="tab-tags" class="tab-panel" role="tabpanel" aria-labelledby="tab-button-tags">
    <h2>标签</h2>
    <label class="check"><input id="tagging-enabled" type="checkbox">启用标签</label>
    <label>谁可以投票<select id="tagging-scope"><option value="disabled">禁用</option><option value="anonymous">匿名访客</option><option value="token">仅管理员</option></select></label>
    <label>分类名称（每行一个）<textarea id="tagging-categories" rows="5" placeholder="男生&#10;女生&#10;AI&#10;风景&#10;动漫"></textarea></label>
    <label class="check"><input id="filter-enabled" type="checkbox">允许访客按标签筛选</label>
    <div class="actions">
      <button id="tagging-stats" class="secondary" type="button">查看统计</button>
      <button id="tagging-reset-path" class="secondary" type="button">清除指定图片</button>
      <button id="tagging-reset-all" class="danger" type="button">清除全部标签</button>
    </div>
    <div id="tagging-stats-result" class="markdown-preview"></div>
    <h3>垃圾桶</h3>
    <p class="note">删除会从 OpenList 永久去掉原图，不可撤销。列表会加载缩略图预览。</p>
    <div class="actions">
      <button id="trash-load" class="secondary" type="button">刷新列表</button>
      <button id="trash-delete-selected" class="danger" type="button">删除选中</button>
      <button id="trash-delete-all" class="danger" type="button">删除全部</button>
    </div>
    <div id="trash-list" class="trash-list"><p class="note">点击“刷新列表”加载。</p></div>
    <div id="trash-result" class="markdown-preview"></div>
  </div>

  <div id="tab-tools" class="tab-panel" role="tabpanel" aria-labelledby="tab-button-tools">
    <h2>维护模式 / 索引 / 备份</h2>
    <h3>维护模式</h3>
    <label class="check"><input id="maintenance-enabled" type="checkbox">启用维护模式</label>
    <p class="note">开启后主界面只显示“维护中”，管理员可用令牌临时解锁。</p>
    <label>日志级别<select id="log-level"><option value="DEBUG">DEBUG</option><option value="INFO">INFO</option><option value="WARNING">WARNING</option><option value="ERROR">ERROR</option></select></label>
    <h3>图片索引</h3>
    <p id="rebuild-status" class="note" aria-live="polite">无数据</p>
    <div class="actions"><button id="rebuild" type="button">后台重建图片索引</button></div>
    <h3>配置备份</h3>
    <div class="actions"><button id="backup" class="secondary" type="button">下载配置备份</button></div>
    <label>上传备份（ZIP）<input id="backup-file" type="file" accept=".zip,application/zip"></label>
    <div class="actions"><button id="restore-backup" class="secondary" type="button">恢复备份</button></div>
    <p class="note">恢复只覆盖本页可编辑项，不含管理密钥和 OpenList 令牌。</p>
  </div>
  <div class="sticky-save actions"><button id="save-server" type="button">保存服务器配置</button></div>
</section>
</div>
<script>
let config=null;
let rebuildTimer=null;
let rebuildJustFinished=false;
const adminStatus=document.querySelector('#admin-status');
const rebuildStatus=document.querySelector('#rebuild-status');
function setAdminStatus(text){adminStatus.textContent=text;adminStatus.classList.toggle('hidden',!text);}
function auth(){return {'Content-Type':'application/json','X-OpenList-Admin-Token':document.querySelector('#token').value};}
function showSelected(){const root=document.querySelector('#selected');root.replaceChildren(...config.directories.map(path=>{const item=document.createElement('div');item.className='selected-item';const text=document.createElement('span');text.textContent=path;const remove=document.createElement('button');remove.className='secondary';remove.type='button';remove.textContent='移除';remove.onclick=()=>{config.directories=config.directories.filter(value=>value!==path);showSelected();};item.append(text,remove);return item;}));}
function escapeHtml(value){return value.replace(/[&<>"']/g,character=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));}
function renderMarkdown(value){return escapeHtml(value).replace(/&lt;font\s+color=(?:&quot;|&#39;)?(#[0-9a-f]{3,8}|[a-z]+)(?:&quot;|&#39;)?\s*&gt;/gi,'<span style="color:$1">').replace(/&lt;\/font&gt;/gi,'</span>').replace(/```([\s\S]*?)```/g,'<pre><code>$1</code></pre>').replace(/^### (.*)$/gm,'<h3>$1</h3>').replace(/^## (.*)$/gm,'<h2>$1</h2>').replace(/^# (.*)$/gm,'<h1>$1</h1>').replace(/`([^`]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>').replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+)\)/g,'<img src="$2" alt="$1" loading="lazy" referrerpolicy="no-referrer">').replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>').replace(/\n\n/g,'</p><p>').replace(/\n/g,'<br>');}
function previewAnnouncement(){document.querySelector('#announcement-preview').innerHTML='<p>'+renderMarkdown(document.querySelector('#announcement-content').value)+'</p>';}
function showAdmin(){document.querySelector('#default-caption').value=config.caption_mode;document.querySelector('#directory-display-enabled').checked=config.directory_display_enabled;document.querySelector('#directory-display-depth').value=config.directory_display_depth;document.querySelector('#theme').value=config.theme||'dark';document.querySelector('#announcement-enabled').checked=config.announcement_enabled;document.querySelector('#announcement-title').value=config.announcement_title;document.querySelector('#announcement-content').value=config.announcement_content;document.querySelector('#announcement-required-seconds').value=config.announcement_required_seconds;document.querySelector('#contact-enabled').checked=config.contact_enabled||false;document.querySelector('#contact-label').value=config.contact_label||'联系';document.querySelector('#contact-qq-number').value=config.contact_qq_number||'';document.querySelector('#contact-qq-url').value=config.contact_qq_url||'';document.querySelector('#contact-qr-url').value=config.contact_qr_url||'';document.querySelector('#maintenance-enabled').checked=config.maintenance_enabled;document.querySelector('#tagging-enabled').checked=config.tagging_enabled||false;document.querySelector('#tagging-scope').value=config.tagging_scope||'disabled';document.querySelector('#tagging-categories').value=(config.tagging_categories||[]).join('\n');document.querySelector('#filter-enabled').checked=config.filter_enabled!==false;document.querySelector('#log-level').value=config.log_level||'INFO';document.querySelector('#protected').classList.remove('hidden');showSelected();previewAnnouncement();refreshRebuildStatus().catch(report);}
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
    const toggle=document.createElement('button');
    toggle.type='button';
    toggle.className='tree-toggle';
    toggle.setAttribute('aria-label','展开 '+name);
    toggle.setAttribute('aria-expanded','false');
    if(hasChildren===false){toggle.textContent='';toggle.classList.add('tree-leaf');toggle.disabled=true;}
    else{toggle.textContent='+';}
    const check=document.createElement('input');
    check.type='checkbox';
    check.className='tree-check';
    const checkId='directory-'+Math.random().toString(36).slice(2);
    check.id=checkId;
    check.checked=config.directories.includes(path);
    check.onchange=()=>{if(check.checked)addDirectory(path);else removeDirectory(path);};
    const label=document.createElement('label');
    label.className='tree-label';
    label.htmlFor=checkId;
    label.textContent=name;
    toggle.onclick=()=>{
      if(hasChildren===false)return;
      node.classList.toggle('open');
      const expanded=node.classList.contains('open');
      toggle.textContent=expanded?'-':'+';
      toggle.setAttribute('aria-expanded',String(expanded));
      if(expanded&&!node.querySelector('.tree-children').children.length){
        loadTreeChildren(path,node).catch(error=>{node.querySelector('.tree-children').innerHTML='<p class="note">加载失败，请折叠后重试。</p>';report(error);});
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
  if(toggle&&!toggle.classList.contains('tree-leaf'))toggle.textContent='-';
  root.append(node);
  const container=node.querySelector('.tree-children');
  if(!data.directories.length){container.innerHTML='<p class="note">当前目录没有子目录。</p>';}
  else{data.directories.forEach(item=>{const child=buildTreeNode(item.name,item.path,true,item.has_children);container.append(child);});}
}
async function saveServer(){if(!config)throw new Error('请先加载服务器配置');const payload={directories:config.directories,caption_mode:document.querySelector('#default-caption').value,directory_display_enabled:document.querySelector('#directory-display-enabled').checked,directory_display_depth:Number(document.querySelector('#directory-display-depth').value),theme:document.querySelector('#theme').value,announcement_enabled:document.querySelector('#announcement-enabled').checked,announcement_title:document.querySelector('#announcement-title').value,announcement_content:document.querySelector('#announcement-content').value,announcement_required_seconds:Number(document.querySelector('#announcement-required-seconds').value),contact_enabled:document.querySelector('#contact-enabled').checked,contact_label:document.querySelector('#contact-label').value,contact_qq_number:document.querySelector('#contact-qq-number').value,contact_qq_url:document.querySelector('#contact-qq-url').value,contact_qr_url:document.querySelector('#contact-qr-url').value,maintenance_enabled:document.querySelector('#maintenance-enabled').checked,tagging_enabled:document.querySelector('#tagging-enabled').checked,tagging_scope:document.querySelector('#tagging-scope').value,tagging_categories:document.querySelector('#tagging-categories').value.split('\n').map(s=>s.trim()).filter(Boolean),filter_enabled:document.querySelector('#filter-enabled').checked,log_level:document.querySelector('#log-level').value};const response=await fetch('/api/admin/config',{method:'PUT',headers:auth(),body:JSON.stringify(payload)});if(!response.ok)throw new Error(await errorText(response,'保存失败'));config=await response.json();showAdmin();setAdminStatus('全局服务器配置已保存；公告修改后将向访客显示新版本。');}
function formatClock(value){const seconds=Math.max(0,Math.round(Number(value)||0));const minutes=String(Math.floor(seconds/60)).padStart(2,'0');const rest=String(seconds%60).padStart(2,'0');return minutes+':'+rest;}
function formatIndexDate(unix){const date=new Date(Number(unix)*1000);if(!unix||Number.isNaN(date.getTime()))return '';const month=String(date.getMonth()+1).padStart(2,'0');const day=String(date.getDate()).padStart(2,'0');return month+'.'+day+' 数据';}
function applyRebuildStatus(status){
  if(!rebuildStatus)return;
  if(status.refreshing){
    rebuildJustFinished=false;
    const elapsed=formatClock((status.index_progress&&status.index_progress.elapsed_seconds)||0);
    const estimate=Number(status.last_build_duration_seconds)||0;
    rebuildStatus.textContent=estimate?'重建中（预计需要 '+formatClock(estimate)+'，已重建 '+elapsed+'）':'重建中（已重建 '+elapsed+'）';
    return;
  }
  if(rebuildJustFinished){
    rebuildStatus.textContent='重建完毕';
    return;
  }
  if(Number(status.image_count||0)<=0){
    rebuildStatus.textContent='无数据';
    return;
  }
  rebuildStatus.textContent=formatIndexDate(status.generated_at)||'无数据';
}
async function refreshRebuildStatus(){
  const response=await fetch('/api/status',{cache:'no-store'});
  if(!response.ok)return null;
  const status=await response.json();
  applyRebuildStatus(status);
  if(status.refreshing&&!rebuildTimer){
    rebuildTimer=setTimeout(()=>pollRebuild().catch(report),2000);
  }
  return status;
}
async function pollRebuild(doneMessage='索引后台重建完成'){
  const status=await refreshRebuildStatus();
  if(!status)return;
  if(status.refreshing){
    rebuildTimer=setTimeout(()=>pollRebuild(doneMessage).catch(report),2000);
    return;
  }
  rebuildJustFinished=true;
  applyRebuildStatus(status);
  setAdminStatus(doneMessage);
  rebuildTimer=null;
  setTimeout(()=>{
    if(rebuildJustFinished){
      rebuildJustFinished=false;
      refreshRebuildStatus().catch(report);
    }
  },2000);
}
async function rebuild(){const previous=await refreshRebuildStatus()||{};const response=await fetch('/api/admin/rebuild',{method:'POST',headers:auth()});if(!response.ok)throw new Error(await errorText(response,'重建未启动'));rebuildJustFinished=false;const estimate=Number(previous.last_build_duration_seconds)||0;setAdminStatus('索引正在后台重建'+(estimate?'，预计约 '+formatClock(estimate):'，首次重建暂无预估时间'));applyRebuildStatus({refreshing:true,last_build_duration_seconds:estimate,index_progress:{elapsed_seconds:0}});clearTimeout(rebuildTimer);rebuildTimer=setTimeout(()=>pollRebuild().catch(report),2000);}
async function backup(){const response=await fetch('/api/admin/backup',{headers:auth()});if(!response.ok)throw new Error(await errorText(response,'备份下载失败'));const blob=await response.blob();const link=document.createElement('a');link.href=URL.createObjectURL(blob);link.download='openlist-image-api-backup.zip';link.click();setTimeout(()=>URL.revokeObjectURL(link.href),1000);setAdminStatus('配置备份已下载（不含 token）');}
async function restoreBackup(){const file=document.querySelector('#backup-file').files[0];if(!file)throw new Error('请先选择 ZIP 备份文件');if(!window.confirm('确定恢复该备份中的可编辑配置吗？'))return;const response=await fetch('/api/admin/backup',{method:'POST',headers:{'X-OpenList-Admin-Token':document.querySelector('#token').value},body:file});if(!response.ok)throw new Error(await errorText(response,'备份恢复失败'));config=await response.json();showAdmin();setAdminStatus('备份配置已恢复，请按需保存或重建图片索引。');}
function report(error){setAdminStatus('操作失败：'+error.message);}
function activateTab(btn){document.querySelectorAll('.tab-button').forEach(b=>{const active=b===btn;b.classList.toggle('active',active);b.setAttribute('aria-selected',String(active));b.tabIndex=active?0:-1;});document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.id==='tab-'+btn.dataset.tab));}
document.querySelectorAll('.tab-button').forEach(btn=>{btn.onclick=()=>activateTab(btn);btn.onkeydown=event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const tabs=[...document.querySelectorAll('.tab-button')];let index=tabs.indexOf(btn);if(event.key==='Home')index=0;else if(event.key==='End')index=tabs.length-1;else index=(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;activateTab(tabs[index]);tabs[index].focus();};});
function applyTheme(theme){document.body.classList.toggle('theme-light',theme==='light');document.body.classList.toggle('theme-dark',theme!=='light');const button=document.querySelector('#theme-toggle');button.textContent=theme==='light'?'☀':'🌙';button.setAttribute('aria-pressed',String(theme==='light'));try{localStorage.setItem('openlist-admin-theme',theme);}catch(e){}}
function toggleTheme(){applyTheme(document.body.classList.contains('theme-light')?'dark':'light');}
document.querySelector('#theme-toggle').onclick=toggleTheme;
(function(){let saved='dark';try{saved=localStorage.getItem('openlist-admin-theme')||'dark';}catch(e){}applyTheme(saved);})();
async function runButtonAction(button,action){if(button.disabled)return;button.disabled=true;button.setAttribute('aria-busy','true');try{await action();}catch(error){report(error);}finally{button.disabled=false;button.removeAttribute('aria-busy');}}
document.querySelector('#load').onclick=event=>runButtonAction(event.currentTarget,load);
document.querySelector('#announcement-preview-button').onclick=previewAnnouncement;
document.querySelector('#browse').onclick=event=>runButtonAction(event.currentTarget,browse);
document.querySelector('#save-server').onclick=event=>runButtonAction(event.currentTarget,saveServer);
document.querySelector('#rebuild').onclick=event=>runButtonAction(event.currentTarget,rebuild);
document.querySelector('#backup').onclick=event=>runButtonAction(event.currentTarget,backup);
document.querySelector('#restore-backup').onclick=event=>runButtonAction(event.currentTarget,restoreBackup);
document.querySelector('#tagging-stats').onclick=event=>runButtonAction(event.currentTarget,loadTagStats);
document.querySelector('#tagging-reset-path').onclick=event=>runButtonAction(event.currentTarget,resetTagPath);
document.querySelector('#tagging-reset-all').onclick=event=>runButtonAction(event.currentTarget,resetTagAll);
document.querySelector('#trash-load').onclick=event=>runButtonAction(event.currentTarget,loadTrashList);
document.querySelector('#trash-delete-selected').onclick=event=>runButtonAction(event.currentTarget,deleteTrashSelected);
document.querySelector('#trash-delete-all').onclick=event=>runButtonAction(event.currentTarget,deleteTrashAll);
async function loadTagStats(){const response=await fetch('/api/tagging/categories',{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'获取统计失败'));const data=await response.json();const result=document.querySelector('#tagging-stats-result');const cats=data.categories||{};const keys=Object.keys(cats);if(!keys.length){result.innerHTML='<p class="note">暂无标签数据。访客投票或打分类后，这里会显示统计。</p>';setAdminStatus('标签统计：暂无数据');return;}const items=keys.map(k=>'<li>'+escapeHtml(k)+'：'+cats[k]+' 张图片</li>').join('');result.innerHTML='<p>当前标签使用情况：</p><ul>'+items+'</ul>';setAdminStatus('标签统计已加载');}
async function resetTagPath(){const path=prompt('请输入要清除标签的图片路径：');if(!path)return;const response=await fetch('/api/admin/tagging/reset?path='+encodeURIComponent(path),{method:'POST',headers:auth()});if(!response.ok)throw new Error(await errorText(response,'清除失败'));setAdminStatus('已清除 '+path+' 的标签数据');}
async function resetTagAll(){if(!confirm('确定清除全部标签数据吗？此操作不可撤销！'))return;const response=await fetch('/api/admin/tagging/reset',{method:'POST',headers:auth()});if(!response.ok)throw new Error(await errorText(response,'清除失败'));setAdminStatus('全部标签数据已清除');}
async function loadTrashList(){const response=await fetch('/api/admin/tagging/trash',{headers:auth(),cache:'no-store'});if(!response.ok)throw new Error(await errorText(response,'获取垃圾列表失败'));const data=await response.json();const list=document.querySelector('#trash-list');const paths=data.paths||[];if(!paths.length){list.innerHTML='<p class="note">暂无垃圾图片标记。</p>';setAdminStatus('垃圾列表：暂无数据');return;}list.replaceChildren();const thumbs=new Map();for(let offset=0;offset<paths.length;offset+=50){try{const resolved=await fetch('/api/download-url',{method:'POST',headers:auth(),body:JSON.stringify({paths:paths.slice(offset,offset+50),preview:true})});if(!resolved.ok)continue;const payload=await resolved.json();(payload.images||[]).forEach(image=>{if(image&&image.path)thumbs.set(image.path,image.thumbnail||image.url||'');});}catch(error){}}paths.forEach(p=>{const item=document.createElement('div');item.className='trash-item';const check=document.createElement('input');check.type='checkbox';check.value=p;const id='trash-'+Math.random().toString(36).slice(2);check.id=id;const thumb=document.createElement('img');thumb.className='trash-thumb';thumb.alt='';thumb.loading='lazy';const src=thumbs.get(p);if(src)thumb.src=src;else thumb.style.visibility='hidden';const label=document.createElement('label');label.htmlFor=id;label.className='trash-path';label.textContent=p;item.append(check,thumb,label);list.append(item);});setAdminStatus('垃圾列表已加载，共 '+paths.length+' 张图片');}
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
