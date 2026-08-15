#!/usr/bin/env python3
"""Setup script to create test environment for tag feature local preview."""
import json
import os
from pathlib import Path

STATE_DIR = Path("/tmp/openlist-tag-preview")
CONFIG_PATH = STATE_DIR / "config.json"
TOKENS_DIR = STATE_DIR / "tokens"
APP_STATE_DIR = STATE_DIR / "state"
IMAGE_DIR = STATE_DIR / "images"

TOKENS_DIR.mkdir(parents=True, exist_ok=True)
APP_STATE_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_DIR.mkdir(parents=True, exist_ok=True)

config = {
    "listen_host": "127.0.0.1",
    "listen_port": 8792,
    "openlist_api_url": "http://127.0.0.1:5244",
    "openlist_token_file": str(TOKENS_DIR / "openlist.token"),
    "admin_token_file": str(TOKENS_DIR / "admin.token"),
    "state_dir": str(APP_STATE_DIR),
    "directories": ["/"],
    "extensions": [".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif", ".bmp", ".svg"],
    "tagging_enabled": True,
    "tagging_scope": "anonymous",
    "tagging_categories": ["壁纸", "人像", "风景", "抽象", "动漫"],
    "tagging_allow_custom": True,
    "tagging_sort_default": "likes",
    "theme": "dark",
}

with open(CONFIG_PATH, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

(TOKENS_DIR / "admin.token").write_text("tag-preview-admin-123456", encoding="utf-8")
(TOKENS_DIR / "openlist.token").write_text("mock-openlist-token", encoding="utf-8")

print(f"Config: {CONFIG_PATH}")
print(f"Admin token: tag-preview-admin-123456")
print(f"OpenList token: mock-openlist-token")
print(f"State dir: {APP_STATE_DIR}")
print(f"Image dir: {IMAGE_DIR}")
