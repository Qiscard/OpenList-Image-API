from __future__ import annotations

import inspect
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openlist_image_api import Application, admin_html, gallery_html  # noqa: E402
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
        self.assertEqual(exported["grid_scale"], 125)


class WebUiMarkupTests(unittest.TestCase):
    def test_gallery_supports_incremental_grid_and_single_cache(self) -> None:
        page = gallery_html()
        self.assertIn("requestImages(25)", page)
        self.assertIn("requestImages(5)", page)
        self.assertIn("singleImages", page)
        self.assertIn("document.documentElement.scrollHeight*.8", page)
        self.assertIn("caption_mode", page)

    def test_admin_starts_with_visitor_config_then_loads_admin_config(self) -> None:
        page = admin_html()
        self.assertIn("/api/public-config", page)
        self.assertIn("grid_gap", page)
        self.assertIn("grid_scale", page)
        self.assertIn("/api/admin/config", page)
        self.assertIn("caption_mode", page)


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
        self.assertNotIn("res.oplist.org", installer)
        self.assertNotRegex(installer, r"\bdocker(?:-compose)?\s+(?:run|start|stop|rm|ps|pull|compose)\b")


if __name__ == "__main__":
    unittest.main()
