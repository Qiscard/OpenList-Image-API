from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openlist_image_api import (  # noqa: E402
    Application,
    OpenListClient,
    SharedImageChain,
    admin_token_from_headers,
    build_index,
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
        self.assertEqual(defaults["url_cache_ttl_seconds"], 7200)
        self.assertEqual(validate_config({"url_cache_size": 8000, "url_cache_ttl_seconds": 7200})["url_cache_size"], 8000)
        with self.assertRaises(ValueError):
            validate_config({"url_cache_size": 8001})
        with self.assertRaises(ValueError):
            validate_config({"url_cache_ttl_seconds": 7201})
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


class OpenListClientRetryTests(unittest.TestCase):
    def make_client(self, token_path: Path) -> OpenListClient:
        token_path.write_text("test-token", encoding="utf-8")
        return OpenListClient(
            {
                "openlist_api_url": "http://127.0.0.1:5244",
                "openlist_token_file": str(token_path),
            }
        )

    def test_file_resolve_does_not_retry_non_throttled_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = self.make_client(Path(temporary) / "openlist.token")
            error = HTTPError(client.base_url, 500, "upstream failed", {}, io.BytesIO())
            with mock.patch("openlist_image_api.urlopen", side_effect=error) as request:
                with self.assertRaises(RuntimeError):
                    client.resolve_file("/gallery/a.jpg")
            self.assertEqual(request.call_count, 1)

    def test_file_resolve_retries_throttled_error_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            client = self.make_client(Path(temporary) / "openlist.token")
            error = HTTPError(client.base_url, 429, "throttled", {}, io.BytesIO())
            response = mock.MagicMock()
            response.__enter__.return_value = io.StringIO(
                json.dumps(
                    {
                        "code": 200,
                        "data": {
                            "raw_url": "https://example.invalid/gallery/a.jpg",
                            "thumb": "https://example.invalid/thumb/gallery/a.jpg",
                        },
                    }
                )
            )
            with mock.patch("openlist_image_api.urlopen", side_effect=[error, response]) as request:
                with mock.patch("openlist_image_api.time.sleep") as sleep:
                    resolved = client.resolve_file("/gallery/a.jpg")
            self.assertEqual(request.call_count, 2)
            sleep.assert_called_once_with(1.0)
            self.assertEqual(resolved[0], "https://example.invalid/gallery/a.jpg")


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

    def test_url_cache_survives_restart_from_disk(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = 0

            def resolve_file(self, path: str) -> tuple[str, str]:
                self.calls += 1
                return "https://example.invalid" + path, "https://example.invalid/thumb" + path

        with tempfile.TemporaryDirectory() as temporary:
            persist = Path(temporary) / "url_cache.json"
            first = UrlCache(8, 3600, persist)
            first.resolve("/gallery/a.jpg", Client())
            first._flush()
            self.assertTrue(persist.exists())
            second = UrlCache(8, 3600, persist)
            client = Client()
            url, thumb = second.resolve("/gallery/a.jpg", client)
            self.assertEqual(client.calls, 0)
            self.assertEqual(url, "https://example.invalid/gallery/a.jpg")
            self.assertEqual(thumb, "https://example.invalid/thumb/gallery/a.jpg")
            self.assertEqual(second.status()["size"], 1)

    def test_url_cache_remembers_thumbnail_without_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            persist = Path(temporary) / "url_cache.json"
            cache = UrlCache(8, 3600, persist)
            cache.remember("/gallery/a.jpg", "", "https://example.invalid/thumb/a.jpg")
            cached = cache.cached_pair("/gallery/a.jpg")
            self.assertEqual(cached, ("", "https://example.invalid/thumb/a.jpg"))
            cache._flush()
            restored = UrlCache(8, 3600, persist)
            self.assertEqual(restored.cached_thumb("/gallery/a.jpg"), "https://example.invalid/thumb/a.jpg")

    def test_preview_resolve_fills_missing_thumbnail_without_returning_original(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.preview_calls = 0

            def resolve_preview(self, path: str) -> tuple[str, str]:
                self.preview_calls += 1
                return "", "https://example.invalid/thumb" + path

        cache = UrlCache(8, 60)
        cache.remember("/gallery/a.jpg", "https://example.invalid/gallery/a.jpg", "")
        client = Client()
        self.assertEqual(
            cache.resolve_preview("/gallery/a.jpg", client),
            ("", "https://example.invalid/thumb/gallery/a.jpg"),
        )
        self.assertEqual(client.preview_calls, 1)
        self.assertEqual(
            cache.cached_pair("/gallery/a.jpg"),
            (
                "https://example.invalid/gallery/a.jpg",
                "https://example.invalid/thumb/gallery/a.jpg",
            ),
        )

    def test_same_path_preview_misses_are_coalesced(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls = 0
                self.lock = threading.Lock()

            def resolve_preview(self, path: str) -> tuple[str, str]:
                with self.lock:
                    self.calls += 1
                time.sleep(0.05)
                return "", "https://example.invalid/thumb" + path

        client = Client()
        cache = UrlCache(8, 60)
        with ThreadPoolExecutor(max_workers=6) as executor:
            results = list(executor.map(lambda _: cache.resolve_preview("/gallery/a.jpg", client), range(6)))
        self.assertEqual(client.calls, 1)
        self.assertEqual(len(set(results)), 1)

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

    def test_resolve_images_lazy_does_not_prefetch(self) -> None:
        class FakeCache:
            def __init__(self) -> None:
                self.hits = 0

            def cached_pair(self, path: str) -> tuple[str, str] | None:
                if path.endswith("/cached.jpg"):
                    self.hits += 1
                    return "https://example.invalid" + path, "https://example.invalid/thumb" + path
                return None

        application = Application.__new__(Application)
        application.cache = FakeCache()
        application.tags = mock.Mock()
        application.prefetch_urls = mock.Mock()
        resolved = application.resolve_images_lazy(
            [{"path": "/gallery/cached.jpg", "size": 1}, {"path": "/gallery/miss.jpg", "size": 2}]
        )
        self.assertEqual(resolved[0]["url"], "https://example.invalid/gallery/cached.jpg")
        self.assertFalse(resolved[0]["needs_url"])
        self.assertEqual(resolved[1]["url"], "")
        self.assertTrue(resolved[1]["needs_url"])
        application.prefetch_urls.assert_not_called()
        application.cache.cached_pair = lambda path: ("", "https://example.invalid/thumb" + path)
        thumb_only = application.resolve_images_lazy([{"path": "/gallery/thumb-only.jpg", "size": 1}])
        self.assertEqual(thumb_only[0]["url"], "")
        self.assertEqual(thumb_only[0]["thumbnail"], "https://example.invalid/thumb/gallery/thumb-only.jpg")
        self.assertFalse(thumb_only[0]["needs_url"])

    def test_resolve_download_urls_truncates_and_keeps_missing_paths(self) -> None:
        class FakeCache:
            def resolve(self, path: str, client: object, refresh: bool = False) -> tuple[str, str]:
                del client, refresh
                if path.endswith("/fail.jpg"):
                    raise RuntimeError("openlist unavailable")
                return "https://example.invalid" + path, "https://example.invalid/thumb" + path

        class FakeRepository:
            def load(self) -> dict[str, object]:
                return {
                    "images": [
                        {"path": "/gallery/ok.jpg", "size": 12},
                        {"path": "/gallery/fail.jpg", "size": 12},
                    ]
                }

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
            self.assertEqual(missing[0]["error"], "image is not in the current index")
            self.assertEqual(missing[1]["error"], "unable to resolve image URL")
            self.assertEqual(missing[2]["url"], "https://example.invalid/gallery/ok.jpg")
        finally:
            application.url_executor.shutdown(wait=True)

    def test_resolve_download_urls_returns_ready_paths_when_some_are_slow(self) -> None:
        finished = threading.Event()

        class FakeCache:
            def resolve(self, path: str, client: object, refresh: bool = False) -> tuple[str, str]:
                del client, refresh
                if path.endswith("/slow.jpg"):
                    time.sleep(0.2)
                    finished.set()
                return "https://example.invalid" + path, ""

        class FakeRepository:
            def load(self) -> dict[str, object]:
                return {
                    "images": [
                        {"path": "/gallery/fast.jpg", "size": 1},
                        {"path": "/gallery/slow.jpg", "size": 1},
                    ]
                }

        application = Application.__new__(Application)
        application.config = {
            "openlist_api_url": "http://127.0.0.1:5244",
            "openlist_token_file": "/tmp/openlist.token",
        }
        application.repository = FakeRepository()
        application.cache = FakeCache()
        application.url_executor = ThreadPoolExecutor(max_workers=2)
        try:
            with mock.patch("openlist_image_api.URL_RESOLVE_WAIT_SECONDS", 0.05):
                started = time.perf_counter()
                resolved = application.resolve_download_urls(["/gallery/fast.jpg", "/gallery/slow.jpg"])
                elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.15)
            by_path = {item["path"]: item for item in resolved}
            self.assertEqual(by_path["/gallery/fast.jpg"]["url"], "https://example.invalid/gallery/fast.jpg")
            self.assertEqual(by_path["/gallery/slow.jpg"]["error"], "url resolve timed out")
            self.assertTrue(finished.wait(0.5))
        finally:
            application.url_executor.shutdown(wait=True)

    def test_resolve_preview_urls_do_not_require_original(self) -> None:
        class FakeCache:
            def resolve_preview(self, path: str, client: object, refresh: bool = False) -> tuple[str, str]:
                del client, refresh
                return "", "https://example.invalid/thumb" + path

        class FakeRepository:
            def load(self) -> dict[str, object]:
                return {"images": [{"path": "/gallery/ok.jpg", "size": 1}]}

        application = Application.__new__(Application)
        application.config = {
            "openlist_api_url": "http://127.0.0.1:5244",
            "openlist_token_file": "/tmp/openlist.token",
        }
        application.repository = FakeRepository()
        application.cache = FakeCache()
        application.url_executor = ThreadPoolExecutor(max_workers=2)
        try:
            resolved = application.resolve_preview_urls(["/gallery/ok.jpg"])
        finally:
            application.url_executor.shutdown(wait=True)
        self.assertEqual(resolved[0]["url"], "")
        self.assertEqual(resolved[0]["thumbnail"], "https://example.invalid/thumb/gallery/ok.jpg")
        self.assertNotIn("error", resolved[0])

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
    def test_list_directories_reads_openlist_live(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = Application.__new__(Application)
            application.config = validate_config({"directories": ["/gallery/sub"]})
            application.repository = IndexRepository(Path(temporary))
            entries = [
                {"name": "b-folder", "is_dir": True},
                {"name": "a-folder", "is_dir": True},
                {"name": "image.jpg", "is_dir": False, "size": 10},
                {"name": "nested", "is_dir": True},
            ]
            with mock.patch.object(OpenListClient, "list_directory", return_value=entries):
                result = application.list_directories("/gallery")
            self.assertEqual(
                result,
                [
                    {"name": "a-folder", "path": "/gallery/a-folder"},
                    {"name": "b-folder", "path": "/gallery/b-folder"},
                    {"name": "nested", "path": "/gallery/nested"},
                ],
            )

    def test_root_listing_falls_back_to_configured_directories_when_openlist_fails(self) -> None:
        application = Application.__new__(Application)
        application.config = validate_config({"directories": ["/gallery/sub"]})
        with mock.patch.object(OpenListClient, "list_directory", side_effect=RuntimeError("down")):
            result = application.list_directories("/")
        self.assertEqual(result, [{"name": "sub", "path": "/gallery/sub"}])

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

    def test_build_index_uses_bounded_concurrency_and_retries_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = validate_config({"directories": ["/root"]})
            config["state_dir"] = temporary
            repository = IndexRepository(Path(temporary))
            active = 0
            maximum = 0
            calls: dict[str, int] = {}
            lock = threading.Lock()

            def list_directory(self: OpenListClient, path: str, index_scan: bool = False) -> list[dict[str, object]]:
                nonlocal active, maximum
                del self, index_scan
                with lock:
                    calls[path] = calls.get(path, 0) + 1
                    active += 1
                    maximum = max(maximum, active)
                time.sleep(0.01)
                try:
                    if path == "/root/bad":
                        raise RuntimeError("permanent timeout")
                    if path == "/root":
                        return [
                            {"name": "a", "is_dir": True},
                            {"name": "bad", "is_dir": True},
                            {"name": "picture.jpg", "is_dir": False, "size": 4},
                            {"name": "..", "is_dir": True},
                        ]
                    if path == "/root/a":
                        return [{"name": "nested.png", "is_dir": False, "size": 8}]
                    return []
                finally:
                    with lock:
                        active -= 1

            with mock.patch.object(OpenListClient, "list_directory", list_directory):
                index = build_index(config, repository)
            self.assertLessEqual(maximum, 4)
            self.assertEqual(calls["/root/bad"], 2)
            self.assertEqual(
                {image["path"] for image in index["images"]},
                {"/root/picture.jpg", "/root/a/nested.png"},
            )
            self.assertEqual(index["errors"], [{"directory": "/root/bad", "error": "permanent timeout"}])
            self.assertFalse((Path(temporary) / "index.checkpoint.json").exists())

    def test_build_index_resumes_matching_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = validate_config({"directories": ["/root"]})
            config["state_dir"] = temporary
            repository = IndexRepository(Path(temporary))
            checkpoint = Path(temporary) / "index.checkpoint.json"
            from openlist_image_api import _index_config_fingerprint
            checkpoint.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "fingerprint": _index_config_fingerprint(config),
                        "started_at": time.time(),
                        "queue": ["/root/resumed"],
                        "visited": ["/root"],
                        "images": [{"path": "/root/already.jpg", "size": 2}],
                        "retry_pending": [],
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(OpenListClient, "list_directory", return_value=[]):
                index = build_index(config, repository)
            self.assertEqual(index["image_count"], 1)
            self.assertEqual(index["images"], [{"path": "/root/already.jpg", "size": 2}])
            self.assertFalse(checkpoint.exists())

    def test_build_index_ignores_checkpoint_when_fingerprint_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = validate_config({"directories": ["/root"]})
            config["state_dir"] = temporary
            repository = IndexRepository(Path(temporary))
            checkpoint = Path(temporary) / "index.checkpoint.json"
            checkpoint.write_text(json.dumps({"fingerprint": "stale", "queue": ["/stale"], "visited": [], "images": []}), encoding="utf-8")
            with mock.patch.object(OpenListClient, "list_directory", return_value=[] ) as list_directory:
                index = build_index(config, repository)
            list_directory.assert_called_once_with("/root", True)
            self.assertEqual(index["images"], [])
            self.assertFalse(checkpoint.exists())

    def test_build_index_ignores_corrupt_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = validate_config({"directories": ["/root"]})
            config["state_dir"] = temporary
            repository = IndexRepository(Path(temporary))
            checkpoint = Path(temporary) / "index.checkpoint.json"
            checkpoint.write_text("{not-json", encoding="utf-8")
            with mock.patch.object(OpenListClient, "list_directory", return_value=[]):
                index = build_index(config, repository)
            self.assertEqual(index["directory_count"], 1)
            self.assertFalse(checkpoint.exists())

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


class SharedImageChainTests(unittest.TestCase):
    def test_same_offset_is_shared(self) -> None:
        chain = SharedImageChain(length=5, ttl_seconds=60)
        first, info = chain.slice(8, 42, 3, offset=0)
        second, same = chain.slice(8, 42, 3, chain_id=info["id"], offset=0)
        self.assertEqual(first, second)
        self.assertEqual(info["id"], same["id"])
        self.assertEqual(same["offset"], 3)
        self.assertEqual(same["remaining"], 2)
        self.assertEqual(len(set(first)), 3)

    def test_visitors_paginate_independently(self) -> None:
        chain = SharedImageChain(length=6, ttl_seconds=60)
        start, info = chain.slice(10, 7, 2, offset=0)
        later, later_info = chain.slice(10, 7, 2, chain_id=info["id"], offset=2)
        restart, restart_info = chain.slice(10, 7, 2, chain_id=info["id"], offset=0)
        self.assertEqual(start, restart)
        self.assertNotEqual(start, later)
        self.assertEqual(later_info["offset"], 4)
        self.assertEqual(restart_info["id"], info["id"])

    def test_exhausted_chain_rotates_but_previous_stays_readable(self) -> None:
        chain = SharedImageChain(length=4, ttl_seconds=60)
        first, info = chain.slice(12, 3, 4, offset=0)
        self.assertEqual(info["remaining"], 0)
        next_chain, rotated = chain.slice(12, 3, 2, chain_id=info["id"], offset=4)
        self.assertTrue(rotated["rotated"])
        self.assertNotEqual(rotated["id"], info["id"])
        continued, previous = chain.slice(12, 3, 2, chain_id=info["id"], offset=2)
        self.assertEqual(previous["id"], info["id"])
        self.assertEqual(continued, first[2:4])
        new_visitor, current = chain.slice(12, 3, 2)
        self.assertEqual(current["id"], rotated["id"])
        self.assertEqual(new_visitor, next_chain)

    def test_ttl_expiry_starts_a_new_chain(self) -> None:
        chain = SharedImageChain(length=4, ttl_seconds=1)
        _first, info = chain.slice(8, 1, 2, offset=0)
        chain._current["expires_at"] = time.monotonic() - 1
        _later, rotated = chain.slice(8, 1, 2)
        self.assertTrue(rotated["rotated"])
        self.assertNotEqual(rotated["id"], info["id"])

    def test_wall_clock_window_rotates_together(self) -> None:
        chain = SharedImageChain(length=4, ttl_seconds=7200)
        with mock.patch.object(chain, "_window_id", return_value=10):
            with mock.patch.object(chain, "_seconds_until_window_end", return_value=7200):
                _first, info = chain.slice(8, 1, 2, offset=0)
        with mock.patch.object(chain, "_window_id", return_value=11):
            with mock.patch.object(chain, "_seconds_until_window_end", return_value=7200):
                _later, rotated = chain.slice(8, 1, 2)
        self.assertTrue(rotated["rotated"])
        self.assertNotEqual(rotated["id"], info["id"])
        self.assertIn("-11-", rotated["id"])

    def test_unfiltered_requests_share_the_same_chain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = Application(Path(temporary) / "config.json")
            images = [{"path": f"/gallery/{index}.jpg", "size": index} for index in range(8)]
            application.repository.save(
                {
                    "images": images,
                    "directories": ["/gallery"],
                    "directory_count": 1,
                    "generated_at": 99,
                    "errors": [],
                }
            )
            first, info = application.choose_images(3, None, None, None, offset=0, use_shared_chain=True)
            second, _same = application.choose_images(3, None, None, None, offset=0, chain_id=info["id"], use_shared_chain=True)
            next_batch, _next = application.choose_images(3, None, None, None, offset=3, chain_id=info["id"], use_shared_chain=True)
            self.assertEqual([image["path"] for image in first], [image["path"] for image in second])
            self.assertEqual(len({image["path"] for image in first + next_batch}), 6)
            with mock.patch("openlist_image_api.random.sample", side_effect=lambda pool, count: pool[:count]) as sample:
                independent, chain_info = application.choose_images(2, None, None, None)
            self.assertIsNone(chain_info)
            self.assertEqual([image["path"] for image in independent], ["/gallery/0.jpg", "/gallery/1.jpg"])
            sample.assert_called_once()
            application.url_executor.shutdown(wait=True)

    def test_filtered_requests_stay_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = Application(Path(temporary) / "config.json")
            images = [{"path": f"/gallery/{index}.jpg", "size": index} for index in range(6)]
            application.repository.save(
                {
                    "images": images,
                    "directories": ["/gallery"],
                    "directory_count": 1,
                    "generated_at": 7,
                    "errors": [],
                }
            )
            with mock.patch("openlist_image_api.random.sample", side_effect=lambda pool, count: pool[:count]) as sample:
                selected, chain_info = application.choose_images(2, "/gallery", None, None, use_shared_chain=True)
            self.assertIsNone(chain_info)
            self.assertEqual([image["path"] for image in selected], ["/gallery/0.jpg", "/gallery/1.jpg"])
            sample.assert_called_once()
            application.url_executor.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
