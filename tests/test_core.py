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
        self.assertEqual(defaults["url_cache_size"], 1000)
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

            def resolve_file(self, path: str) -> str:
                with self.lock:
                    self.calls += 1
                time.sleep(0.05)
                return "https://example.invalid" + path

        client = Client()
        cache = UrlCache(100, 60)
        with ThreadPoolExecutor(max_workers=8) as executor:
            urls = list(executor.map(lambda _: cache.resolve("/gallery/a.jpg", client), range(8)))
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(set(urls)), 1)
        self.assertEqual(cache.status()["misses"], 1)

    def test_batch_url_resolution_runs_in_parallel(self) -> None:
        class SlowCache:
            def __init__(self) -> None:
                self.active = 0
                self.maximum = 0
                self.lock = threading.Lock()

            def resolve(self, path: str, client: object) -> str:
                del client
                with self.lock:
                    self.active += 1
                    self.maximum = max(self.maximum, self.active)
                time.sleep(0.04)
                with self.lock:
                    self.active -= 1
                return "https://example.invalid" + path

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
