#!/usr/bin/env python3
"""Test tag voting API endpoints against the local preview server."""
import json
import sys
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8792"
TIMEOUT = 5

passed = 0
failed = 0


def ok(label):
    global passed
    passed += 1
    print(f"  PASS: {label}")


def fail(label, detail=""):
    global failed
    failed += 1
    print(f"  FAIL: {label} {detail}")


def post(path, body, headers=None):
    data = json.dumps(body).encode("utf-8")
    h = {"Content-Type": "application/json", "Content-Length": str(len(data))}
    if headers:
        h.update(headers)
    req = urllib.request.Request(BASE + path, data=data, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body.decode("utf-8", "replace")}
        return e.code, parsed
    except Exception as e:
        return -1, {"error": str(e)}


def get(path, headers=None):
    req = urllib.request.Request(BASE + path, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = {"raw": body.decode("utf-8", "replace")}
        return e.code, parsed
    except Exception as e:
        return -1, {"error": str(e)}


print("=== Step 1: Get index paths from /api/status ===")
status, data = get("/api/status")
if status != 200:
    fail("get status", f"status={status} data={data}")
    print("\nServer not reachable. Is the preview server running on port 8792?")
    sys.exit(1)
print(f"  Index: {data.get('index', {}).get('count', '?')} images")
ok("status reachable")

print("\n=== Step 2: Get random images (with tags) ===")
status, data = get("/api/images/random?count=2")
if status != 200:
    fail("get random images", f"status={status} data={data}")
    sys.exit(1)

images = data.get("images", [])
if len(images) < 2:
    fail("need at least 2 images")
    sys.exit(1)

path1 = images[0]["path"]
path2 = images[1]["path"]
for img in images:
    tags = img.get("tags", {})
    print(f"  {img['path']} -> likes={tags.get('likes', 0)} dislikes={tags.get('dislikes', 0)} cats={tags.get('categories', [])}")
ok("random images returned with tags field")

print(f"\n=== Step 3: Vote LIKE on {path1} ===")
status, result = post("/api/tagging/vote", {"path": path1, "type": "like", "value": True})
print(f"  Status: {status}, Result: {result}")
if status == 200 and result.get("likes", 0) >= 1:
    ok("like vote registered")
else:
    fail("like vote", f"status={status} result={result}")

print(f"\n=== Step 4: Vote LIKE again (toggle off) on {path1} ===")
status, result = post("/api/tagging/vote", {"path": path1, "type": "like", "value": True})
print(f"  Status: {status}, Result: {result}")
if status == 200 and result.get("likes", 0) == 0:
    ok("like toggled off")
else:
    fail("toggle off", f"status={status} result={result}")

print(f"\n=== Step 5: Vote LIKE (re-enable) on {path1} ===")
status, result = post("/api/tagging/vote", {"path": path1, "type": "like", "value": True})
print(f"  Status: {status}, Result: {result}")
if status == 200 and result.get("likes", 0) == 1:
    ok("like re-enabled")
else:
    fail("re-enable like", f"status={status} result={result}")

print(f"\n=== Step 6: Vote DISLIKE on {path1} (should cancel like) ===")
status, result = post("/api/tagging/vote", {"path": path1, "type": "dislike", "value": True})
print(f"  Status: {status}, Result: {result}")
if status == 200 and result.get("likes", 0) == 0 and result.get("dislikes", 0) == 1:
    ok("dislike cancels like")
else:
    fail("dislike cancels like", f"status={status} result={result}")

print(f"\n=== Step 7: Add category to {path2} ===")
status, result = post("/api/tagging/vote", {"path": path2, "type": "category", "category": "wallpapers", "value": True})
print(f"  Status: {status}, Result: {result}")
if status == 200 and "wallpapers" in result.get("categories", []):
    ok("category added")
else:
    fail("add category", f"status={status} result={result}")

print(f"\n=== Step 8: Add custom category to {path2} ===")
status, result = post("/api/tagging/vote", {"path": path2, "type": "category", "category": "custom-test", "value": True})
print(f"  Status: {status}, Result: {result}")
if status == 200 and "custom-test" in result.get("categories", []):
    ok("custom category added")
else:
    fail("add custom category", f"status={status} result={result}")

print(f"\n=== Step 9: Remove category from {path2} ===")
status, result = post("/api/tagging/vote", {"path": path2, "type": "category", "category": "wallpapers", "value": False})
print(f"  Status: {status}, Result: {result}")
if status == 200 and "wallpapers" not in result.get("categories", []):
    ok("category removed")
else:
    fail("remove category", f"status={status} result={result}")

print(f"\n=== Step 10: Get tag stats ===")
status, result = get(f"/api/tagging/stats?paths={path1},{path2}")
print(f"  Status: {status}, Result: {result}")
if status == 200 and "stats" in result:
    ok("stats returned")
else:
    fail("get stats", f"status={status} result={result}")

print(f"\n=== Step 11: Get all categories ===")
status, result = get("/api/tagging/categories")
print(f"  Status: {status}, Result: {result}")
if status == 200 and "categories" in result:
    ok("categories returned")
else:
    fail("get categories", f"status={status} result={result}")

print(f"\n=== Step 12: Filter images by tag 'custom-test' ===")
status, result = get("/api/images/random?count=5&tag=custom-test")
print(f"  Status: {status}")
if status == 200:
    found = [img["path"] for img in result.get("images", [])]
    print(f"  Filtered images: {found}")
    if path2 in found:
        ok("tag filter returns tagged image")
    else:
        fail("tag filter should include tagged image")
else:
    fail("tag filter", f"status={status} result={result}")

print(f"\n=== Step 13: Invalid vote type (should 400) ===")
status, result = post("/api/tagging/vote", {"path": path1, "type": "invalid", "value": True})
if status == 400:
    ok("invalid vote type rejected")
else:
    fail("invalid vote type", f"status={status}")

print(f"\n=== Step 14: Missing path (should 400) ===")
status, result = post("/api/tagging/vote", {"type": "like", "value": True})
if status == 400:
    ok("missing path rejected")
else:
    fail("missing path", f"status={status}")

print(f"\n=== Step 15: Non-indexed path (should 400) ===")
status, result = post("/api/tagging/vote", {"path": "/nonexistent/image.svg", "type": "like", "value": True})
if status == 400:
    ok("non-indexed path rejected")
else:
    fail("non-indexed path", f"status={status}")

print(f"\n{'='*50}")
print(f"RESULTS: {passed} passed, {failed} failed")
if failed == 0:
    print("ALL TESTS PASSED")
else:
    print("SOME TESTS FAILED")
    sys.exit(1)
