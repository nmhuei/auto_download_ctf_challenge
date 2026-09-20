"""Unit tests for 'ctf platform' CLI commands (list, show, probe, add, remove)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ctf_downloader.cli import build_unified_parser
from ctf_downloader.cli_commands import handle_platform
from ctf_downloader.platforms.registry import PLATFORMS
from ctf_downloader.platforms.schema_store import PlatformSchemaStore


class TestPlatformCliCommands(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ws_path = Path(self.tmp_dir.name)
        self.parser = build_unified_parser()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_platform_list_runs_cleanly(self):
        args = self.parser.parse_args(["platform", "list", "-w", str(self.ws_path)])
        # Should execute without raising
        handle_platform(args)

    def test_platform_show_builtin_and_adapter(self):
        # Show metactf (builtin schema)
        args1 = self.parser.parse_args(["platform", "show", "metactf", "-w", str(self.ws_path)])
        handle_platform(args1)

        # Show ctfd (builtin adapter)
        args2 = self.parser.parse_args(["platform", "show", "ctfd"])
        handle_platform(args2)

    def test_platform_show_missing_raises_exit(self):
        args = self.parser.parse_args(["platform", "show", "non_existent_platform_999"])
        with self.assertRaises(SystemExit) as ctx:
            handle_platform(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_platform_add_and_remove_lifecycle(self):
        sample_json = json.dumps({
            "key": "test_cli_add_ctf",
            "label": "Test CLI Add CTF",
            "endpoints": {"challenges": "/api/v1/challs"},
        })
        args_add = self.parser.parse_args(["platform", "add", sample_json, "--scope", "workspace", "-w", str(self.ws_path)])
        handle_platform(args_add)

        # Verify added to schema store
        schema = PlatformSchemaStore.get("test_cli_add_ctf", workspace_path=self.ws_path)
        self.assertIsNotNone(schema)
        self.assertEqual(schema.label, "Test CLI Add CTF")

        # Remove
        args_rm = self.parser.parse_args(["platform", "remove", "test_cli_add_ctf", "--scope", "workspace", "-w", str(self.ws_path)])
        handle_platform(args_rm)

        schema_after = PlatformSchemaStore.get("test_cli_add_ctf", workspace_path=self.ws_path)
        self.assertIsNone(schema_after)

    @mock.patch("ctf_downloader.platforms.recon.safe_get")
    @mock.patch("ctf_downloader.platforms.recon.safe_get_json")
    def test_platform_probe_with_save(self, mock_safe_get_json, mock_safe_get):
        mock_root = mock.MagicMock()
        mock_root.status_code = 200
        mock_root.text = "<html><head><title>Probed CTF</title></head></html>"
        mock_safe_get.return_value = mock_root

        def fake_json(sess, url, statuses=(200,)):
            if "/api/v1/challenges" in url:
                return {"data": {"challenges": [{"id": 1, "name": "C1"}]}}, 200
            return None, 404

        mock_safe_get_json.side_effect = fake_json

        args_probe = self.parser.parse_args([
            "platform", "probe", "https://probed-ctf.example.org",
            "--key", "probed_test_ctf",
            "--save",
            "--scope", "workspace",
            "-w", str(self.ws_path),
        ])
        handle_platform(args_probe)

        # Check saved
        saved = PlatformSchemaStore.get("probed_test_ctf", workspace_path=self.ws_path)
        self.assertIsNotNone(saved)
        self.assertEqual(saved.label, "Probed CTF")
        self.assertEqual(saved.endpoints.challenges, "/api/v1/challenges")


if __name__ == "__main__":
    unittest.main()
