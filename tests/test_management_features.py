from __future__ import annotations

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

from openlist_image_api import Application  # noqa: E402
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


class TuiStatusTests(unittest.TestCase):
    def test_activating_service_status_does_not_crash(self) -> None:
        config = {"listen_port": 8790, "directories": []}
        status = {"image_count": 0, "directory_count": 0, "refreshing": False, "last_refresh_error": ""}
        output = io.StringIO()
        with (
            patch.object(openlist_tui, "read_config", return_value=config),
            patch.object(openlist_tui, "command_output", return_value="activating"),
            patch.object(openlist_tui, "request_status", return_value=status),
            redirect_stdout(output),
        ):
            openlist_tui.show_status()
        self.assertIn("图片 API 服务: activating", output.getvalue())


if __name__ == "__main__":
    unittest.main()
