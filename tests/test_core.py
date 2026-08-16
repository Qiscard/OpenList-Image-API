from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openlist_image_api import (  # noqa: E402
    Application,
    admin_token_from_headers,
    IndexRepository,
    UrlCache,
    is_loopback_openlist_url,
    normalize_directories,
    normalize_directory,
    validate_config,
)


class ConfigurationTests(unittest.TestCase):
    def test_normalize_virtual_directory(self) -> None:
        self.assertEqual(normalize_directory("/gallery//summer/"), "/gallery/summer")
        self.assertEqual(normalize_directories(["/gallery", "gallery", "/archive"]), ["/gallery", "/archive"])
        with self.assertRaises(ValueError):
            normalize_directory("/gallery/../private")

    def test_openlist_url_is_loopback_only(self) -> None:
        self.assertTrue(is_loopback_openlist_url("http://127.0.0.1:5244"))
        self.assertTrue(is_loopback_openlist_url("http://localhost:5244"))
        self.assertFalse(is_loopback_openlist_url("https://example.invalid"))
        self.assertFalse(is_loopback_openlist_url("http://not-local.invalid:5244"))

    def test_admin_token_header_accepts_webui_and_legacy_names(self) -> None:
        self.assertEqual(admin_token_from_headers({"X-OpenList-Admin-Token": "webui-token"}), "webui-token")
        self.assertEqual(admin_token_from_headers({"X-Admin-Token": "legacy-token"}), "legacy-token")
        self.assertIsNone(admin_token_from_headers({"X-OpenList-Admin-Token": ""}))

    def test_configuration_allows_nat_listener_and_validates_gallery_options(self) -> None:
        defaults = validate_config({})
        self.assertEqual(defaults["listen_host"], "0.0.0.0")
        self.assertEqual(defaults["caption_mode"], "path")
        self.assertEqual(defaults["grid_gap"], 12)
        self.assertEqual(defaults["grid_scale"], 150)
        self.assertEqual(defaults["url_cache_size"], 0)
        self.assertEqual(defaults["url_cache_ttl_seconds"], 1800)
        self.assertEqual(validate_config({"listen_host": "0.0.0.0"})["listen_host"], "0.0.0.0")
        configured = validate_config({"caption_mode": "name", "grid_gap": 0, "grid_scale": 200})
        self.assertEqual(configured["caption_mode"], "name")
        with self.assertRaises(ValueError):
            validate_config({"listen_host": "localhost"})
        with self.assertRaises(ValueError):
            validate_config({"caption_mode": "url"})
        with self.assertRaises(ValueError):
            validate_config({"grid_scale": 74})
        with self.assertRaises(ValueError):
            validate_config({"openlist_api_url": "http://example.invalid:5244"})


class UrlCacheConcurrencyTests(unittest.TestCase):
    def test_same_path_concurrent_misses_are_coalesced(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = 0
                self.lock = threading.Lock()

            def resolve_file(self, path: str) -> tuple[str, str]:
                with self.lock:
                    self.calls += 1
                time.sleep(0.05)
                return "https://example.invalid" + path, "https://example.invalid/thumb" + path

        client = Client()
        cache = UrlCache(100, 60)
        with ThreadPoolExecutor(max_workers=8) as executor:
            urls = list(executor.map(lambda _: cache.resolve("/gallery/a.jpg", client), range(8)))
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(set(urls)), 1)
        self.assertEqual(cache.status()["misses"], 1)

    def test_disabled_cache_still_singleflights_same_path(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = 0
                self.lock = threading.Lock()

            def resolve_file(self, path: str) -> tuple[str, str]:
                with self.lock:
                    self.calls += 1
                time.sleep(0.05)
                return "https://example.invalid" + path, "https://example.invalid/thumb" + path

        client = Client()
        cache = UrlCache(0, 60)
        with ThreadPoolExecutor(max_workers=8) as executor:
            urls = list(executor.map(lambda _: cache.resolve("/gallery/a.jpg", client), range(8)))
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(set(urls)), 1)
        self.assertEqual(cache.status()["size"], 0)
        self.assertEqual(cache.status()["misses"], 1)

    def test_concurrent_refresh_shares_one_openlist_lookup(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = 0
                self.lock = threading.Lock()

            def resolve_file(self, path: str) -> tuple[str, str]:
                with self.lock:
                    self.calls += 1
                time.sleep(0.05)
                return "https://example.invalid" + path, "https://example.invalid/thumb" + path

        client = Client()
        cache = UrlCache(8, 60)
        cache.resolve("/gallery/a.jpg", client)
        self.assertEqual(client.calls, 1)
        with ThreadPoolExecutor(max_workers=6) as executor:
            urls = list(executor.map(lambda _: cache.resolve("/gallery/a.jpg", client, refresh=True), range(6)))
        self.assertEqual(client.calls, 2)
        self.assertEqual(len(set(urls)), 1)

    def test_prefetch_and_download_url_share_inflight(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class Client:
            def __init__(self) -> None:
                self.calls = 0
                self.lock = threading.Lock()

            def resolve_file(self, path: str) -> tuple[str, str]:
                with self.lock:
                    self.calls += 1
                started.set()
                release.wait(1)
                return "https://example.invalid" + path, "https://example.invalid/thumb" + path

        client = Client()
        cache = UrlCache(0, 60)
        results: list[tuple[str, str]] = []

        def prefetch() -> None:
            results.append(cache.resolve("/gallery/a.jpg", client))

        worker = threading.Thread(target=prefetch)
        worker.start()
        self.assertTrue(started.wait(1))
        results.append(cache.resolve("/gallery/a.jpg", client))
        release.set()
        worker.join(1)
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])

    def test_batch_url_resolution_runs_in_parallel(self) -> None:
        class SlowCache:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.lock = threading.Lock()

            def resolve(self, path: str, client: object) -> tuple[str, str]:
                del client
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(0.04)
                with self.lock:
                    self.active -= 1
                return "https://example.invalid" + path, "https://example.invalid/thumb" + path

        application = Application.__new__(Application)
        application.config = {
            "openlist_api_url": "http://127.0.0.1:5244",
            "openlist_token_file": "/tmp/openlist.token",
        }
        application.cache = SlowCache()
        application.url_executor = ThreadPoolExecutor(max_workers=4)
        try:
            images = [{"path": f"/gallery/{index}.jpg", "size": index} for index in range(8)]
            resolved = application.resolve_images(images)
        finally:
            application.url_executor.shutdown(wait=True)
        self.assertEqual(len(resolved), 8)
        self.assertGreater(application.cache.maximum, 1)

    def test_indexed_images_scans_index_once(self) -> None:
        class CountingRepository:
            def __init__(self) -> None:
                self.loads = 0

            def load(self) -> dict[str, object]:
                self.loads += 1
                return {
                    "images": [
                        {"path": f"/gallery/{index}.jpg", "size": index}
                        for index in range(8)
                    ]
                }

        application = Application.__new__(Application)
        application.repository = CountingRepository()
        resolved = application.indexed_images(
            ["/gallery/1.jpg", "/gallery/3.jpg", "/missing.jpg", "/gallery/3.jpg", "../bad"]
        )
        self.assertEqual(application.repository.loads, 1)
        self.assertEqual(
            [image["path"] for image in resolved],
            ["/gallery/1.jpg", "/gallery/3.jpg", "/missing.jpg"],
        )
        self.assertTrue(resolved[2]["_missing"])
        self.assertEqual(application.indexed_image("/gallery/1.jpg")["size"], 1)
        with self.assertRaises(ValueError):
            application.indexed_image("/missing.jpg")

    def test_resolve_download_urls_truncates_and_keeps_missing_paths(self) -> None:
        class FakeCache:
            def resolve(self, path: str, client: object, refresh: bool = False) -> tuple[str, str]:
                del client, refresh
                if path.endswith("/fail.jpg"):
                    raise RuntimeError("openlist unavailable")
                return "https://example.invalid" + path, "https://example.invalid/thumb" + path

        class FakeRepository:
            def load(self) -> dict[str, object]:
                return {"images": [{"path": "/gallery/ok.jpg", "size": 12}]}

        application = Application.__new__(Application)
        application.config = {
            "openlist_api_url": "http://127.0.0.1:5244",
            "openlist_token_file": "/tmp/openlist.token",
        }
        application.repository = FakeRepository()
        application.cache = FakeCache()
        application.url_executor = ThreadPoolExecutor(max_workers=4)
        try:
            paths = [f"/gallery/{index}.jpg" for index in range(51)] + ["/gallery/fail.jpg", "/gallery/ok.jpg"]
            resolved = application.resolve_download_urls(paths)
            self.assertEqual(len(resolved), 50)
            self.assertTrue(all("url" in item or "error" in item for item in resolved))
            missing = application.resolve_download_urls(["/gallery/missing.jpg", "/gallery/fail.jpg", "/gallery/ok.jpg"])
            self.assertEqual(missing[0]["path"], "/gallery/missing.jpg")
            self.assertIn("url", missing[0])
            self.assertEqual(missing[1]["error"], "unable to resolve image URL")
            self.assertEqual(missing[2]["url"], "https://example.invalid/gallery/ok.jpg")
        finally:
            application.url_executor.shutdown(wait=True)

    def test_non_cache_config_reload_preserves_url_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.json"
            config = validate_config({})
            config_path.write_text(json.dumps(config), encoding="utf-8")
            application = Application(config_path)
            original_cache = application.cache
            config["directories"] = ["/gallery"]
            config_path.write_text(json.dumps(config), encoding="utf-8")
            application.reload_config()
            application.url_executor.shutdown(wait=True)
        self.assertIs(application.cache, original_cache)


class IndexRepositoryTests(unittest.TestCase):
    def test_index_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = IndexRepository(Path(temporary))
            expected = {
                "images": [{"path": "/gallery/a.jpg", "size": 1}],
                "directories": ["/gallery"],
                "generated_at": 1,
                "errors": [],
            }
            repository.save(expected)
            self.assertEqual(repository.load(), expected)

    def test_status_exposes_last_index_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = Application.__new__(Application)
            application.config = validate_config({})
            application.config["grid_scale"] = 125
            application.repository = IndexRepository(Path(temporary))
            application.cache = UrlCache(0, 0)
            application.refreshing = False
            application.last_refresh_error = ""
            application.repository.save(
                {
                    "images": [],
                    "directories": [],
                    "directory_count": 0,
                    "generated_at": 1,
                    "build_duration_seconds": 12.5,
                    "errors": [],
                }
            )
            self.assertEqual(application.status()["last_build_duration_seconds"], 12.5)


if __name__ == "__main__":
    unittest.main()
