from __future__ import annotations

import inspect
import io
import json
import sys
import tempfile
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import call, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openlist_image_api import TRASH_TAG, Application, admin_html, gallery_html, make_handler  # noqa: E402
import openlist_tui  # noqa: E402


class BackupTests(unittest.TestCase):
    def test_backup_excludes_secret_file_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = Application(Path(temporary) / "config.json")
            archive_bytes = application.create_config_backup()
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            payload = json.loads(archive.read("openlist-image-api-config.json"))
        exported = payload["config"]
        self.assertNotIn("openlist_token_file", exported)
        self.assertNotIn("admin_token_file", exported)
        self.assertIn("directories", exported)
        self.assertNotIn("view_layout", exported)
        self.assertNotIn("delivery", exported)
        self.assertEqual(exported["caption_mode"], "path")
        self.assertNotIn("grid_gap", exported)
        self.assertNotIn("grid_scale", exported)


class AdminConfigurationTests(unittest.TestCase):
    def test_admin_config_only_exposes_global_server_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = Application(Path(temporary) / "config.json")
            self.assertEqual(
                set(application.admin_config()),
                {
                    "directories",
                    "caption_mode",
                    "directory_display_enabled",
                    "directory_display_depth",
                    "theme",
                    "announcement_enabled",
                    "announcement_title",
                    "announcement_content",
                    "announcement_required_seconds",
                    "announcement_version",
                    "maintenance_enabled",
                    "tagging_enabled",
                    "tagging_scope",
                    "tagging_categories",
                    "tagging_allow_custom",
                    "tagging_sort_default",
                    "filter_enabled",
                    "log_level",
                },
            )
            self.assertEqual(
                set(application.visitor_config()),
                {
                    "view_layout",
                    "grid_gap",
                    "caption_mode",
                    "directory_display_enabled",
                    "directory_display_depth",
                    "theme",
                    "announcement",
                    "maintenance_enabled",
                    "filter_enabled",
                    "tagging",
                },
            )
            self.assertNotIn("delivery", application.visitor_config())
            self.assertNotIn("grid_scale", application.visitor_config())
            updated = application.update_admin_config({"caption_mode": "name"})
            self.assertEqual(updated["caption_mode"], "name")
            self.assertEqual(application.visitor_config()["caption_mode"], "name")
            with self.assertRaises(ValueError):
                application.update_admin_config({"view_layout": "grid"})
            application.url_executor.shutdown(wait=True)

    def test_announcement_version_bumps_on_content_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            application = Application(Path(temporary) / "config.json")
            baseline = application.admin_config()["announcement_version"]
            application.update_admin_config({"announcement_content": "新版公告"})
            self.assertEqual(application.admin_config()["announcement_version"], baseline + 1)
            application.update_admin_config({"caption_mode": "name"})
            self.assertEqual(application.admin_config()["announcement_version"], baseline + 1)
            application.url_executor.shutdown(wait=True)


class WebUiMarkupTests(unittest.TestCase):
    def test_gallery_uses_slideshow_and_stable_waterfall(self) -> None:
        page = gallery_html()
        self.assertIn("requestImages(WATERFALL_BATCH_SIZE)", page)
        self.assertIn("WATERFALL_BATCH_SIZE=20", page)
        self.assertIn("SLIDE_PRELOAD_COUNT=2", page)
        self.assertIn("URL_RESOLVE_CONCURRENCY=3", page)
        self.assertIn("SLIDE_INITIAL_LOAD=6", page)
        self.assertIn("loadSlideshow", page)
        self.assertIn("document.documentElement.scrollHeight*.6", page)
        self.assertIn("queueImageResolve(current,{priority:0})", page)
        self.assertIn("refreshImageUrls(upcoming,false,true,1)", page)
        self.assertIn("preloadUpcomingSlides", page)
        self.assertIn("waterfallPrefetchPromise", page)
        self.assertIn("prefetchNextWaterfallBatch()", page)
        self.assertIn("const cards=images.map(image=>createCard(image))", page)
        self.assertNotIn("const first=images.slice(0,4)", page)
        self.assertIn("method:'POST'", page)
        self.assertIn("fetchJsonWithRetry('/api/download-url'", page)
        self.assertIn("preview:true", page)
        self.assertIn("new Map((resolved.images||[]).map", page)
        self.assertIn("批量预览解析失败，将按图片重试", page)
        self.assertIn("const URL_RESOLVE_CONCURRENCY=3", page)
        self.assertIn("urlResolveTasks=new Map()", page)
        self.assertIn("needsImageResolve(image,force,preview)", page)
        self.assertIn("if(needsImageResolve(image,false,true))", page)
        self.assertIn("const sourceReady=preview?!!image.thumbnail:!!image.url", page)
        self.assertIn("generation!==renderGeneration", page)
        self.assertIn("role=\"status\" aria-live=\"polite\"", page)
        self.assertIn("aria-busy=\"true\"", page)
        self.assertIn("if(width<=560) return Number(settings&&settings.mobile_waterfall_columns)||1", page)
        self.assertIn("id=\"mobile-waterfall-columns\"", page)
        self.assertIn("mobile_waterfall_columns", page)
        self.assertIn(".tag-bar{display:flex", page)
        self.assertIn("linear-gradient(180deg,rgba(0,0,0,.78)", page)
        self.assertIn(".tag-chip{gap:3px;padding:3px 8px;font-size:11px}", page)
        self.assertNotIn(".help-panel", page)
        self.assertNotIn("HELP_MARKDOWN", page)
        self.assertNotIn("id=\"help\"", page)
        self.assertNotIn("id=\"help-button\"", page)
        self.assertIn("❤", page)
        self.assertIn("🌙", page)
        self.assertIn("☀", page)
        self.assertIn("🗑️", page)
        self.assertIn(".gallery.waterfall .card img{max-height:none;min-height:0;height:auto}", page)
        self.assertIn("scroll-snap-type:x proximity", page)
        self.assertIn("role=\"dialog\" aria-modal=\"true\" aria-labelledby=\"preferences-title\"", page)
        self.assertIn("prefers-reduced-motion:reduce", page)
        self.assertIn("className='image-error'", page)
        self.assertIn("openlist-image-preferences-v2", page)
        self.assertIn("openlist-image-announcement-v2-", page)
        self.assertIn("themeFab.textContent=theme==='light'?'☀':'🌙'", page)
        self.assertIn("navPause.textContent=slideshowPaused?'播放':'暂停'", page)
        self.assertIn("like.innerHTML='❤ <span class=\"tag-vote-count\">'", page)
        self.assertIn("card.className='card is-loading'", page)
        self.assertIn("picture.alt=''", page)
        self.assertIn(".card.is-loading img", page)
        self.assertIn("body:not(.theme-light)::after", page)
        self.assertEqual(TRASH_TAG, "垃圾桶")
        self.assertIn("waterfall-column", page)
        self.assertIn("shortestWaterfallColumn", page)
        self.assertIn("prioritizeWaterfallImages", page)
        self.assertIn("waterfallRevealObserver", page)
        self.assertIn("preview.onclick=()=>openLightbox(image)", page)
        self.assertIn("picture.fetchPriority='high'", page)
        self.assertIn("/api/public-config", page)
        self.assertIn("设置仅保存在当前浏览器", page)
        self.assertIn("preview-quality", page)
        self.assertIn("lightbox-quality", page)
        self.assertIn("theme-fab", page)
        self.assertIn("sizedThumb", page)
        self.assertIn("cardSrc(image)", page)
        self.assertIn("lightboxSrc(image)", page)
        self.assertNotIn("theme-mode", page)
        self.assertNotIn("settings.delivery", page)
        self.assertNotIn("download.href=downloadUrl", page)
        self.assertNotIn("openlist-image-preferences-v1", page)
        self.assertNotIn("singleImages", page)
        self.assertNotIn(".gallery.grid", page)
        self.assertNotIn("card.classList.toggle('wide'", page)
        self.assertNotIn("preferences.default_", page)

    def test_admin_exposes_announcement_maintenance_and_directory_controls(self) -> None:
        page = admin_html()
        self.assertIn("/api/admin/config", page)
        self.assertIn("#default-caption", page)
        self.assertIn("#directory-display-enabled", page)
        self.assertIn("#directory-display-depth", page)
        self.assertIn("#announcement-enabled", page)
        self.assertIn("#announcement-title", page)
        self.assertIn("#announcement-content", page)
        self.assertIn("#announcement-required-seconds", page)
        self.assertIn("#maintenance-enabled", page)
        self.assertGreater(page.find('id="maintenance-enabled"'), page.find('id="tab-tools"'))
        self.assertIn("id=\"rebuild-status\"", page)
        self.assertIn("重建中（预计需要", page)
        self.assertIn("重建完毕", page)
        self.assertIn("if(Number(status.image_count||0)<=0)", page)
        self.assertIn("rebuildStatus.textContent='无数据'", page)
        self.assertNotIn("/api/admin/logs", page)
        self.assertNotIn("id=\"log-view\"", page)
        self.assertIn("🌙", page)
        self.assertIn("☀", page)
        self.assertIn("className='trash-thumb'", page)
        self.assertIn("preview:true", page)
        self.assertIn("offset+=50", page)
        self.assertIn("body:not(.theme-light)::after", page)
        self.assertIn("#browse", page)
        self.assertIn("实时读取自 OpenList", page)
        self.assertNotIn("refresh-directory-cache", page)
        self.assertNotIn("/api/admin/directories/refresh", page)
        self.assertIn("预计约", page)
        self.assertNotIn("id=\"delivery\"", page)
        self.assertNotIn("id=\"scale\"", page)
        self.assertNotIn("extensions:parsedExtensions()", page)
        self.assertNotIn("id=\"caption\"", page)
        self.assertNotIn("tagging-sort-default", page)
        self.assertIn("后台重建图片索引", page)
        self.assertIn("维护模式 / 索引 / 备份", page)
        self.assertIn("role=\"tablist\"", page)
        self.assertIn("role=\"tab\" aria-selected=\"true\"", page)
        self.assertIn("role=\"tabpanel\"", page)
        self.assertIn("runButtonAction", page)
        self.assertIn("#tagging-stats').onclick=event=>runButtonAction", page)
        self.assertIn("#trash-delete-all').onclick=event=>runButtonAction", page)
        self.assertIn("aria-expanded", page)
        self.assertNotIn("save-server-bottom", page)


class DownloadTests(unittest.TestCase):
    def test_download_streams_an_attachment_instead_of_redirecting(self) -> None:
        body = b"image-content"

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, message: str, *args: object) -> None:
                del message, args

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        upstream_thread.start()

        class FakeApplication:
            config = {"maintenance_enabled": False}

            def is_admin(self, supplied_token: object) -> bool:
                return True

            def indexed_image(self, path: str) -> dict[str, object]:
                return {"path": path, "size": len(body)}

            def resolve_images(self, images: list[dict[str, object]]) -> list[dict[str, object]]:
                return [
                    {
                        **images[0],
                        "url": f"http://127.0.0.1:{upstream.server_port}/image.jpg",
                    }
                ]

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeApplication()))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            with urlopen(f"http://127.0.0.1:{server.server_port}/download?path=/gallery/%E5%9B%BE%E7%89%87.jpg") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), body)
                self.assertEqual(response.headers["Content-Type"], "image/jpeg")
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertIn("filename*=UTF-8''", response.headers["Content-Disposition"])
                self.assertIsNone(response.headers.get("Location"))
        finally:
            server.shutdown()
            server.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_download_url_post_resolves_multiple_paths(self) -> None:
        class FakeApplication:
            config = {"maintenance_enabled": False}

            def is_admin(self, supplied_token: object) -> bool:
                del supplied_token
                return True

            def indexed_image(self, path: str) -> dict[str, object]:
                return {"path": path, "size": 1}

            def resolve_images(self, images: list[dict[str, object]], refresh: bool = False) -> list[dict[str, object]]:
                del refresh
                return [{"path": images[0]["path"], "url": "https://example.invalid" + str(images[0]["path"]), "thumbnail": ""}]

            def resolve_download_urls(self, paths: list[str], refresh: bool = False) -> list[dict[str, object]]:
                del refresh
                return [
                    {"path": path, "url": "https://example.invalid" + path, "thumbnail": "https://example.invalid/thumb" + path}
                    for path in paths
                ]

            def resolve_preview_urls(self, paths: list[str], refresh: bool = False) -> list[dict[str, object]]:
                del refresh
                return [
                    {"path": path, "url": "", "thumbnail": "https://example.invalid/thumb" + path}
                    for path in paths
                ]

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeApplication()))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            request = Request(
                f"http://127.0.0.1:{server.server_port}/api/download-url",
                data=json.dumps({"paths": ["/gallery/a.jpg", "/gallery/b.jpg"], "fresh": False}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                payload = json.loads(response.read().decode("utf-8"))
            self.assertEqual(response.status, 200)
            self.assertEqual(
                payload["images"],
                [
                    {"path": "/gallery/a.jpg", "url": "https://example.invalid/gallery/a.jpg", "thumbnail": "https://example.invalid/thumb/gallery/a.jpg"},
                    {"path": "/gallery/b.jpg", "url": "https://example.invalid/gallery/b.jpg", "thumbnail": "https://example.invalid/thumb/gallery/b.jpg"},
                ],
            )
            with urlopen(f"http://127.0.0.1:{server.server_port}/api/download-url?path=/gallery/a.jpg") as response:
                single = json.loads(response.read().decode("utf-8"))
            self.assertEqual(single["url"], "https://example.invalid/gallery/a.jpg")
            preview_request = Request(
                f"http://127.0.0.1:{server.server_port}/api/download-url",
                data=json.dumps({"paths": ["/gallery/a.jpg"], "preview": True}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(preview_request) as response:
                preview = json.loads(response.read().decode("utf-8"))
            self.assertEqual(preview["images"][0]["url"], "")
            self.assertEqual(preview["images"][0]["thumbnail"], "https://example.invalid/thumb/gallery/a.jpg")
        finally:
            server.shutdown()
            server.server_close()


class TaggingAuthorizationTests(unittest.TestCase):
    def test_token_scope_requires_valid_admin_token(self) -> None:
        class FakeTags:
            def set_category(self, path: str, category: str, value: bool) -> dict[str, object]:
                return {"path": path, "category": category, "value": value}

        class FakeApplication:
            config = {"maintenance_enabled": False, "tagging_enabled": True, "tagging_scope": "token", "tagging_categories": ["review"]}
            TRASH_TAG = "trash"
            tags = FakeTags()

            def is_admin(self, supplied_token: str | None) -> bool:
                return supplied_token == "valid-token"

            def indexed_image(self, path: str) -> dict[str, object]:
                return {"path": path, "size": 1}

            def voter_id(self, ip: str, user_agent: str, admin_token: str | None) -> str:
                del ip, user_agent
                return "voter:" + str(admin_token)

        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(FakeApplication()))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            body = json.dumps({"path": "/gallery/a.jpg", "type": "category", "category": "review", "value": True}).encode("utf-8")
            invalid = Request(
                f"http://127.0.0.1:{server.server_port}/api/tagging/vote",
                data=body,
                headers={"Content-Type": "application/json", "X-OpenList-Admin-Token": "invalid-token"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as error:
                urlopen(invalid)
            self.assertEqual(error.exception.code, 403)

            valid = Request(
                f"http://127.0.0.1:{server.server_port}/api/tagging/vote",
                data=body,
                headers={"Content-Type": "application/json", "X-OpenList-Admin-Token": "valid-token"},
                method="POST",
            )
            with urlopen(valid) as response:
                self.assertEqual(response.status, 200)
        finally:
            server.shutdown()
            server.server_close()


class TuiStatusTests(unittest.TestCase):
    def test_activating_service_status_does_not_crash(self) -> None:
        config = {"listen_host": "0.0.0.0", "listen_port": 8790, "directories": []}
        status = {
            "image_count": 0,
            "directory_count": 0,
            "refreshing": False,
            "last_refresh_error": "",
            "last_build_duration_seconds": 0,
        }
        output = io.StringIO()
        with (
            patch.object(openlist_tui, "read_config", return_value=config),
            patch.object(openlist_tui, "command_output", return_value="activating"),
            patch.object(openlist_tui, "request_status", return_value=status),
            redirect_stdout(output),
        ):
            openlist_tui.show_status()
        self.assertIn("图片 API 服务: activating", output.getvalue())
        self.assertIn("可通过 NAT 转发访问", output.getvalue())


class TuiManagementTests(unittest.TestCase):
    def test_update_uses_embedded_installer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "install.sh"
            installer.touch()
            with (
                patch.object(openlist_tui, "APP_INSTALLER_PATH", installer),
                patch.object(openlist_tui, "require_root"),
                patch.object(openlist_tui, "run") as run_command,
            ):
                openlist_tui.update_application("github")
                openlist_tui.update_application("gitee")
        self.assertEqual(
            run_command.call_args_list,
            [
                call(["bash", str(installer), "--source", "github", "--update"]),
                call(["bash", str(installer), "--source", "gitee", "--update"]),
            ],
        )

    def test_api_uninstall_uses_scoped_installer_action(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            installer = Path(temporary) / "install.sh"
            installer.touch()
            with (
                patch.object(openlist_tui, "APP_INSTALLER_PATH", installer),
                patch.object(openlist_tui, "require_root"),
                patch("builtins.input", side_effect=["1", "YES"]),
                patch.object(openlist_tui, "run") as run_command,
            ):
                openlist_tui.uninstall_application()
        run_command.assert_called_once_with(["bash", str(installer), "--uninstall", "api"])

    def test_service_management_can_stop_image_api(self) -> None:
        with (
            patch.object(openlist_tui, "require_root"),
            patch("builtins.input", side_effect=["3", "", "0"]),
            patch.object(openlist_tui, "run") as run_command,
        ):
            openlist_tui.service_management()
        run_command.assert_called_once_with(["systemctl", "stop", openlist_tui.SERVICE_NAME])

    def test_main_menu_uses_grouped_actions(self) -> None:
        menu_source = inspect.getsource(openlist_tui.main_menu)
        self.assertIn('"4": service_management', menu_source)
        self.assertIn('"5": show_status_with_admin_token', menu_source)
        self.assertIn('"6": maintenance_menu', menu_source)
        self.assertNotIn("rebuild_index", menu_source)
        self.assertNotIn('"7": maintenance_menu', menu_source)
        self.assertNotIn('"12": print_admin_token', menu_source)
        maintenance_source = inspect.getsource(openlist_tui.maintenance_menu)
        self.assertIn("更新项目（github）", maintenance_source)
        self.assertIn("更新项目（gitee）", maintenance_source)
        self.assertIn("全局迁移", maintenance_source)

    def test_cache_cleanup_removes_persisted_url_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "url_cache.json"
            cache_path.write_text("{}", encoding="utf-8")
            with (
                patch.object(openlist_tui, "require_root"),
                patch.object(openlist_tui, "cleanup_legacy_residuals"),
                patch.object(openlist_tui, "read_config", return_value={"state_dir": temporary}),
                patch.object(openlist_tui, "command_output", return_value="inactive"),
            ):
                openlist_tui.cleanup_residuals_and_runtime_cache()
            self.assertFalse(cache_path.exists())

    def test_global_migration_packs_placeholders_without_token_contents(self) -> None:
        import tarfile

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = root / "config.json"
            config_path.write_text('{"state_dir": "%s"}\n' % root.as_posix(), encoding="utf-8")
            (root / "index.json").write_text('{"images":[]}\n', encoding="utf-8")
            (root / "tags.json").write_text("{}\n", encoding="utf-8")
            (root / "url_cache.json").write_text("{}\n", encoding="utf-8")
            (root / "index.checkpoint.json").write_text("{}\n", encoding="utf-8")
            (root / "rebuild.log").write_text("should-not-pack\n", encoding="utf-8")
            token_path = root / "openlist.token"
            admin_path = root / "admin.token"
            token_path.write_text("secret-openlist-token\n", encoding="utf-8")
            admin_path.write_text("secret-admin-token\n", encoding="utf-8")
            output_dir = root / "out"
            output_dir.mkdir()
            with (
                patch.object(openlist_tui, "require_root"),
                patch.object(openlist_tui, "CONFIG_PATH", config_path),
                patch.object(openlist_tui, "TOKEN_PATH", token_path),
                patch.object(openlist_tui, "ADMIN_TOKEN_PATH", admin_path),
                patch.object(openlist_tui, "MIGRATION_DIR", output_dir),
                patch.object(openlist_tui, "read_config", return_value={"state_dir": str(root)}),
            ):
                archive_path = openlist_tui.export_global_migration()
            self.assertTrue(archive_path.is_file())
            with tarfile.open(archive_path, "r:gz") as archive:
                names = set(archive.getnames())
                self.assertTrue({"config.json", "index.json", "tags.json", "url_cache.json", "index.checkpoint.json", "openlist.token", "admin.token"} <= names)
                self.assertNotIn("rebuild.log", names)
                self.assertEqual(archive.extractfile("openlist.token").read(), b"")
                self.assertEqual(archive.extractfile("admin.token").read(), b"")
                packed = archive.extractfile("config.json").read()
                self.assertNotIn(b"secret-openlist-token", packed)
                self.assertNotIn(b"secret-admin-token", packed)


class InstallerTests(unittest.TestCase):
    def test_embedded_installer_uses_fixed_proxy_candidates(self) -> None:
        installer = (Path(__file__).resolve().parents[1] / "install.sh").read_text(encoding="utf-8")
        for proxy in (
            "https://edgeone.gh-proxy.com",
            "https://hk.gh-proxy.com",
            "https://gh-proxy.com",
            "https://gh.dpik.top",
        ):
            self.assertIn(proxy, installer)
        self.assertIn("--retry 2 --retry-all-errors", installer)
        self.assertIn("--connect-timeout 20", installer)
        self.assertIn("--max-time 20", installer)
        self.assertIn("GitHub source unavailable; falling back to Gitee", installer)
        self.assertNotIn("Gitee source unavailable; falling back to GitHub", installer)
        auto_idx = installer.find("auto)")
        github_fetch = installer.find('fetch_from "${GITHUB_RAW_BASE}"', auto_idx)
        gitee_fetch = installer.find('fetch_from "${GITEE_RAW_BASE}"', auto_idx)
        self.assertGreater(github_fetch, auto_idx)
        self.assertGreater(gitee_fetch, github_fetch)
        self.assertIn("--install-openlist", installer)
        self.assertIn("--update", installer)
        self.assertIn("--uninstall api|complete", installer)
        self.assertIn("set_download_ref", installer)
        self.assertIn('"url_cache_size": 1000', installer)
        self.assertIn('"url_cache_ttl_seconds": 1800', installer)
        self.assertIn("migrate_performance_defaults", installer)
        self.assertNotIn("res.oplist.org", installer)
        self.assertNotRegex(installer, r"\bdocker(?:-compose)?\s+(?:run|start|stop|rm|ps|pull|compose)\b")


if __name__ == "__main__":
    unittest.main()
