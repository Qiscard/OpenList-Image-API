#!/usr/bin/env python3
"""Mock OpenList server for local tag feature testing."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

STATE_DIR = Path(os.environ.get("MOCK_STATE_DIR", "/tmp/openlist-tag-preview"))
IMAGE_DIR = STATE_DIR / "images"
INDEX_FILE = STATE_DIR / "mock_index.json"
TOKEN = "mock-openlist-token"

SAMPLE_CATEGORIES = [
    ("wallpapers", "壁纸"),
    ("portraits", "人像"),
    ("landscapes", "风景"),
    ("abstract", "抽象"),
    ("anime", "动漫"),
]


def build_index():
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    index = {"images": [], "directories": []}
    for cat_slug, cat_name in SAMPLE_CATEGORIES:
        cat_dir = IMAGE_DIR / cat_slug
        cat_dir.mkdir(parents=True, exist_ok=True)
        index["directories"].append({"name": cat_slug, "path": "/" + cat_slug, "is_dir": True})
        for i in range(1, 4):
            fname = f"{cat_slug}_{i:03d}.svg"
            fpath = cat_dir / fname
            if not fpath.exists():
                hue = (hash(cat_slug + str(i)) % 360)
                svg = generate_svg(cat_name, i, hue)
                fpath.write_text(svg, encoding="utf-8")
            rel_path = f"/{cat_slug}/{fname}"
            stat = fpath.stat()
            index["images"].append({
                "name": fname,
                "path": rel_path,
                "size": stat.st_size,
                "is_dir": False,
                "parent": "/" + cat_slug,
            })
    INDEX_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    return index


def generate_svg(category, index, hue):
    colors = [
        f"hsl({hue},70%,55%)",
        f"hsl({(hue + 40) % 360},65%,45%)",
        f"hsl({(hue + 80) % 360},60%,65%)",
    ]
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="800" height="600" viewBox="0 0 800 600">
  <defs>
    <linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="{colors[0]}"/>
      <stop offset="50%" stop-color="{colors[1]}"/>
      <stop offset="100%" stop-color="{colors[2]}"/>
    </linearGradient>
  </defs>
  <rect width="800" height="600" fill="url(#g)"/>
  <circle cx="200" cy="180" r="80" fill="rgba(255,255,255,0.25)"/>
  <circle cx="600" cy="420" r="120" fill="rgba(0,0,0,0.15)"/>
  <rect x="350" y="250" width="100" height="100" fill="rgba(255,255,255,0.3)" transform="rotate(45 400 300)"/>
  <text x="400" y="320" font-family="sans-serif" font-size="48" fill="white" text-anchor="middle" font-weight="bold">{category} #{index}</text>
  <text x="400" y="370" font-family="sans-serif" font-size="24" fill="rgba(255,255,255,0.8)" text-anchor="middle">Mock Test Image</text>
</svg>"""


def load_index():
    if not INDEX_FILE.exists():
        return build_index()
    return json.loads(INDEX_FILE.read_text(encoding="utf-8"))


class MockHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        path = urlparse(self.path).path
        token = self.headers.get("Authorization", "")
        if token != TOKEN:
            self._send_json(401, {"code": 401, "message": "invalid token", "data": None})
            return
        index = load_index()
        if path == "/api/fs/list":
            req_path = payload.get("path", "/")
            if req_path == "/" or req_path == "":
                content = [{"name": d["name"], "path": d["path"], "is_dir": True} for d in index["directories"]]
            else:
                prefix = req_path.rstrip("/") + "/"
                content = []
                for img in index["images"]:
                    if img["path"].startswith(prefix):
                        content.append({
                            "name": img["name"],
                            "path": img["path"],
                            "is_dir": False,
                            "size": img["size"],
                            "parent": img.get("parent", req_path),
                        })
            self._send_json(200, {"code": 200, "message": "success", "data": {
                "content": content,
                "total": len(content),
                "page": 1,
                "per_page": 1000,
            }})
        elif path == "/api/fs/get":
            req_path = payload.get("path", "")
            img = next((i for i in index["images"] if i["path"] == req_path), None)
            if not img:
                self._send_json(404, {"code": 404, "message": "file not found", "data": None})
                return
            raw_url = f"http://127.0.0.1:5245/raw{req_path}"
            thumb_url = f"http://127.0.0.1:5245/thumb{req_path}"
            self._send_json(200, {"code": 200, "message": "success", "data": {
                "name": img["name"],
                "path": img["path"],
                "raw_url": raw_url,
                "thumb": thumb_url,
                "size": img["size"],
            }})
        else:
            self._send_json(404, {"code": 404, "message": "not found", "data": None})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith("/raw/") or parsed.path.startswith("/thumb/"):
            rel = parsed.path[5:]
            fpath = IMAGE_DIR / rel
            if fpath.exists() and fpath.suffix == ".svg":
                data = fpath.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def _send_json(self, code, data):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass


def main():
    build_index()
    server = ThreadingHTTPServer(("127.0.0.1", 5244), MockHandler)
    print("Mock OpenList server running on http://127.0.0.1:5244", flush=True)
    print(f"Token: {TOKEN}", flush=True)
    print(f"Images dir: {IMAGE_DIR}", flush=True)
    print(f"Index: {INDEX_FILE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
