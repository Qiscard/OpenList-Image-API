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
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openlist_image_api import Application, admin_html, gallery_html, make_handler  # noqa: E402
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
                    "announcement_enabled",
                    "announcement_title",
                    "announcement_content",
                    "announcement_required_seconds",
                    "announcement_version",
                    "maintenance_enabled",
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
                    "announcement",
                    "maintenance_enabled",
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
        self.assertIn("requestImages(15)", page)
        self.assertIn("SLIDE_PRELOAD_COUNT=3", page)
        self.assertIn("loadSlideshow", page)
        self.assertIn("document.documentElement.scrollHeight*.78", page)
        self.assertIn("openlist-image-preferences-v2", page)
        self.assertIn("openlist-image-announcement-v2-", page)
        self.assertIn("grid-template-columns:repeat(3", page)
        self.assertIn("grid-auto-flow:dense", page)
        self.assertIn("grid-column:span 2", page)
        self.assertIn("naturalHeight>=1.45", page)
        self.assertIn("waterfall-column", page)
        self.assertIn("waterfallAppendIndex%columns.length", page)
        self.assertIn("preview.onclick=()=>openLightbox(image)", page)
        self.assertIn("picture.fetchPriority='high'", page)
        self.assertIn("/api/public-config", page)
        self.assertIn("设置仅保存在当前浏览器", page)
        self.assertNotIn("settings.delivery", page)
        self.assertNotIn("download.href=downloadUrl", page)
        self.assertNotIn("openlist-image-preferences-v1", page)
        self.assertNotIn("singleImages", page)

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
        self.assertIn("refresh-directory-cache", page)
        self.assertIn("预计约", page)
        self.assertNotIn("id=\"delivery\"", page)
        self.assertNotIn("id=\"scale\"", page)
        self.assertNotIn("extensions:parsedExtensions()", page)
        self.assertNotIn("id=\"caption\"", page)


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
                openlist_tui.update_application()
        run_command.assert_called_once_with(["bash", str(installer), "--update"])

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
        self.assertIn('"6": show_status_with_admin_token', menu_source)
        self.assertIn('"7": maintenance_menu', menu_source)
        self.assertNotIn('"12": print_admin_token', menu_source)


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
