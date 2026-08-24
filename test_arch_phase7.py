"""Phase 7 — CLI mỏng + entrypoint shims.

Kiểm tra:
- Script root (main/ctf/submit/manage/instance/rank) là shim ≤10 dòng.
- Entrypoint --help vẫn trả exit 0 với help text nguyên văn (marker key).
- Exit code đúng cho các đường legacy offline (instance no-args -> 1,
  rank thiếu URL -> 1, argparse sai cú pháp -> 2).
- cli layer (cli.py + cli_commands.py) KHÔNG còn input()/Prompt.ask/
  Confirm.ask nào — wizard nằm ở services (AST check).
- handle_workspaces redirect về StatusService.scan_all_workspaces;
  instance --sync gọi InstanceService.sync_containers.
"""
import ast
import os
import subprocess
import sys
import unittest
from argparse import Namespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.abspath(__file__))

SHIMS = ["main.py", "ctf.py", "submit.py", "manage.py", "instance.py", "rank.py"]


def _run(args):
    return subprocess.run(
        [sys.executable] + args, capture_output=True, text=True, timeout=60, cwd=ROOT
    )


class TestEntrypointShims(unittest.TestCase):
    def test_root_scripts_are_thin_shims(self):
        for name in SHIMS:
            with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                lines = [ln for ln in f.read().splitlines() if ln.strip()]
            self.assertLessEqual(
                len(lines), 10, f"{name} phải là shim ≤10 dòng (thấy {len(lines)})"
            )

    def test_legacy_mains_importable(self):
        from ctf_downloader import cli_legacy

        for fn in (
            "legacy_submit_main",
            "legacy_manage_main",
            "legacy_instance_main",
            "legacy_rank_main",
        ):
            self.assertTrue(callable(getattr(cli_legacy, fn)), fn)


class TestHelpSnapshot(unittest.TestCase):
    """Smoke help từng entrypoint — argv/help text không đổi với user."""

    MARKERS = {
        ("main.py", "--help"): "Unified CTF Downloader, Submitter, Container Manager",
        ("main.py", "pull", "--help"): "Target CTF platform URL",
        ("main.py", "status", "--help"): "Show only unsolved challenges",
        ("main.py", "workspaces", "--help"): "Base CTF directory to scan",
        ("main.py", "instance", "--help"): "Interactive container wizard",
        ("main.py", "submit", "--help"): "Auto-scan workspace for filled flags and submit",
        ("main.py", "rank", "--help"): "Number of top teams to display",
        ("submit.py", "--help"): "Automated CTF Flag Submitter",
        ("manage.py", "--help"): "CTF Workspace Challenge Manager & Dashboard",
        ("instance.py", "--help"): "CTF Dynamic Container / Instance Manager",
        ("rank.py", "--help"): "CTF Scoreboard & Live Ranking Manager",
    }

    def test_help_markers_all_entrypoints(self):
        for args, marker in self.MARKERS.items():
            with self.subTest(entrypoint=args):
                r = _run(list(args))
                self.assertEqual(r.returncode, 0, f"{args}: rc={r.returncode}")
                self.assertIn(marker, r.stdout + r.stderr)

    def test_version_flag(self):
        r = _run(["ctf.py", "--version"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("ctf-toolkit 2.0.0", r.stdout + r.stderr)


class TestLegacyExitCodes(unittest.TestCase):
    def test_instance_no_args_prints_help_exit_1(self):
        # workspace hợp lệ -> qua init -> không có action/-l/-i/-n/--id
        # -> parser.print_help() + sys.exit(1) như bản cũ
        r = _run(["instance.py", "-w", "PTIT_CTF_2026"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("CTF Dynamic Container / Instance Manager", r.stdout)

    def test_instance_bad_workspace_init_error_exit_1(self):
        # init lỗi trước khi xử lý args — hành vi giữ nguyên từ bản cũ
        r = _run(["instance.py"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("Initialization error", r.stdout + r.stderr)

    def test_rank_without_url_exit_1(self):
        # workspace '.' không resolve được URL -> RankService raise -> exit 1
        r = _run(["rank.py", "-w", "."])
        self.assertEqual(r.returncode, 1)

    def test_main_invalid_subcommand_exit_2(self):
        r = _run(["main.py", "no-such-command"])
        self.assertEqual(r.returncode, 2)

    def test_instance_missing_challenge_exit_1(self):
        r = _run(["instance.py", "-w", ".", "--id", "999999", "--status"])
        self.assertEqual(r.returncode, 1)


class TestNoPromptInCliLayer(unittest.TestCase):
    def _prompt_calls(self, path):
        tree = ast.parse(open(path, encoding="utf-8").read())
        hits = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "input":
                hits.append(f"input() at line {node.lineno}")
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "ask"
                and isinstance(func.value, ast.Name)
                and func.value.id in ("Prompt", "Confirm")
            ):
                hits.append(f"{func.value.id}.ask() at line {node.lineno}")
        return hits

    def test_no_input_or_prompt_calls_in_cli_layer(self):
        for mod in ("cli.py", "cli_commands.py"):
            path = os.path.join(ROOT, "ctf_downloader", mod)
            self.assertEqual([], self._prompt_calls(path), mod)


class TestCliCommandsDelegation(unittest.TestCase):
    def test_handle_workspaces_redirects_to_status_service(self):
        from ctf_downloader.cli_commands import handle_workspaces
        from ctf_downloader.services.status_service import StatusService

        with patch.object(StatusService, "scan_all_workspaces") as mock_scan:
            handle_workspaces(Namespace(dir="/tmp/fake_ctf"))
        mock_scan.assert_called_once_with("/tmp/fake_ctf")

    def test_handle_status_uses_status_service_render_tree(self):
        from ctf_downloader.cli_commands import handle_status
        from ctf_downloader.services.status_service import StatusService

        with patch.object(StatusService, "render_tree") as mock_rt:
            handle_status(
                Namespace(
                    workspace=".", category=["Web"], unsolved=True,
                    solved=False, container=False,
                )
            )
        _, kwargs = mock_rt.call_args
        self.assertEqual(kwargs.get("filter_cat"), ["Web"])
        self.assertTrue(kwargs.get("only_unsolved"))

    def test_instance_sync_flag_goes_through_service(self):
        from ctf_downloader import cli_legacy
        from ctf_downloader.services.instance_service import InstanceService

        with patch.object(InstanceService, "__init__", return_value=None), \
             patch.object(InstanceService, "sync_containers", return_value=3) as mock_sync:
            with patch.object(sys, "argv", ["instance.py", "-w", "ws", "--sync"]):
                cli_legacy.legacy_instance_main()
        mock_sync.assert_called_once_with()

    def test_handle_instance_list_renders_from_service(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.services.instance_service import InstanceService

        fake = [{"id": 7, "name": "Some Chall", "category": "Web", "solves_count": 12}]
        ns = Namespace(action=None, workspace=".", cookie=None, token=None,
                       id=None, name=None, list=True, interactive=False)
        with patch.object(cli_commands, "get_auth_for_workspace", return_value=(None, None)), \
             patch.object(InstanceService, "__init__", return_value=None), \
             patch.object(InstanceService, "list_containers", return_value=fake):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli_commands.handle_instance(ns)
        out = buf.getvalue()
        self.assertIn("Some Chall", out)
        self.assertIn("7", out)


if __name__ == "__main__":
    unittest.main()
