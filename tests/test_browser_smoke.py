from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openlist_image_api import Application, make_handler  # noqa: E402


def playwright_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


@unittest.skipUnless(playwright_available(), "Playwright Chromium is not available")
class GalleryBrowserSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        temporary = Path(self._temporary.name)
        config_path = temporary / "config.json"
        state_dir = temporary / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (temporary / "openlist.token").write_text("smoke-openlist-token", encoding="utf-8")
        (temporary / "admin.token").write_text("smoke-admin-token", encoding="utf-8")
        config_path.write_text(
            json.dumps(
                {
                    "listen_host": "127.0.0.1",
                    "listen_port": 8790,
                    "openlist_api_url": "http://127.0.0.1:5244",
                    "openlist_token_file": str(temporary / "openlist.token"),
                    "admin_token_file": str(temporary / "admin.token"),
                    "state_dir": str(state_dir),
                    "directories": ["/gallery"],
                    "url_cache_size": 0,
                    "announcement_enabled": False,
                    "contact_enabled": False,
                    "maintenance_enabled": False,
                    "tagging_enabled": False,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.application = Application(config_path)
        images = [{"path": f"/gallery/author-{index // 3}/img-{index:03d}.jpg", "size": 1000 + index} for index in range(12)]
        self.application.repository.save(
            {
                "images": images,
                "directories": ["/gallery"],
                "directory_count": 1,
                "generated_at": 100,
                "build_duration_seconds": 0.1,
                "errors": [],
            }
        )

        def fake_pair(path: str, client: object, refresh: bool = False) -> tuple[str, str]:
            thumb = (
                "data:image/svg+xml,"
                "%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' height='160'%3E"
                "%3Crect width='100%25' height='100%25' fill='%233d8bfd'/%3E"
                f"%3Ctext x='50%25' y='50%25' fill='white' text-anchor='middle' dy='.3em' font-size='12'%3E{path.split('/')[-1]}%3C/text%3E"
                "%3C/svg%3E"
            )
            return thumb, thumb

        self._resolve_patch = mock.patch.object(self.application.cache, "resolve", side_effect=fake_pair)
        self._preview_patch = mock.patch.object(self.application.cache, "resolve_preview", side_effect=fake_pair)
        self._resolve_patch.start()
        self._preview_patch.start()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.application))
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self._preview_patch.stop()
        self._resolve_patch.stop()
        self.server.shutdown()
        self.server.server_close()
        self.application.url_executor.shutdown(wait=False)
        self._temporary.cleanup()

    def test_gallery_loads_public_config_and_waterfall_batch(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            responses: list[dict[str, object]] = []

            def on_response(response) -> None:  # type: ignore[no-untyped-def]
                url = response.url
                if "/api/public-config" in url or "/api/images/random" in url or "/api/download-url" in url:
                    responses.append({"url": url, "status": response.status})

            page.on("response", on_response)
            page.goto(f"{self.base_url}/gallery", wait_until="domcontentloaded", timeout=30000)
            self.assertEqual(page.title(), "图库")
            page.wait_for_function(
                "() => window.fetch && document.querySelector('.gallery')",
                timeout=15000,
            )
            page.wait_for_selector(".gallery .card", timeout=15000)
            page.wait_for_timeout(500)

            public_ok = any(item["status"] == 200 and "/api/public-config" in str(item["url"]) for item in responses)
            random_ok = any(item["status"] == 200 and "/api/images/random" in str(item["url"]) for item in responses)
            self.assertTrue(public_ok, f"public-config missing or failed: {responses}")
            self.assertTrue(random_ok, f"random images missing or failed: {responses}")

            card_count = page.locator(".gallery .card").count()
            self.assertGreaterEqual(card_count, 1, "gallery should render at least one card")
            browser.close()

    def test_admin_page_renders_login_shell(self) -> None:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(f"{self.base_url}/admin", wait_until="domcontentloaded", timeout=30000)
            self.assertEqual(page.title(), "图库管理")
            self.assertGreater(page.locator("#token").count(), 0)
            browser.close()


class WebuiAssetTests(unittest.TestCase):
    def test_webui_files_exist_beside_module(self) -> None:
        from openlist_image_api import admin_html, gallery_html, webui_dir

        directory = webui_dir()
        self.assertTrue((directory / "gallery.html").is_file())
        self.assertTrue((directory / "admin.html").is_file())
        self.assertIn("<!doctype html>", gallery_html().lower())
        self.assertIn("图库管理", admin_html())


if __name__ == "__main__":
    unittest.main()
