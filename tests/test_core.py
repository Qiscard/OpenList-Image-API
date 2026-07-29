from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openlist_image_api import (  # noqa: E402
    IndexRepository,
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

    def test_configuration_rejects_public_listener_and_remote_api(self) -> None:
        with self.assertRaises(ValueError):
            validate_config({"listen_host": "0.0.0.0"})
        with self.assertRaises(ValueError):
            validate_config({"openlist_api_url": "http://example.invalid:5244"})


class IndexRepositoryTests(unittest.TestCase):
    def test_index_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = IndexRepository(Path(temporary))
            expected = {"images": [{"path": "/gallery/a.jpg", "size": 1}], "directories": ["/gallery"], "generated_at": 1, "errors": []}
            repository.save(expected)
            self.assertEqual(repository.load(), expected)


if __name__ == "__main__":
    unittest.main()
