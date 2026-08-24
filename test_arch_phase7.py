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


class TestStorageCommand(unittest.TestCase):
    """Phase 7b — ``ctf storage`` (alias du/archive): parse 2 subpath + mock
    StorageManager assert đúng tham số + exit code non-tty."""

    def _parser(self):
        from ctf_downloader.cli import build_unified_parser

        return build_unified_parser()

    # ---- parse args: subpath scan ----
    def test_parse_scan_defaults(self):
        ns = self._parser().parse_args(["storage"])
        self.assertEqual(ns.base_dir, os.path.expanduser("~/Workspace/CTF"))
        self.assertEqual(ns.threshold_mb, 1024)
        self.assertIsNone(ns.storage_command)

    def test_parse_scan_custom_flags(self):
        ns = self._parser().parse_args(
            ["storage", "-d", "/tmp/fake_ctf", "--threshold-mb", "256"]
        )
        self.assertEqual(ns.base_dir, "/tmp/fake_ctf")
        self.assertEqual(ns.threshold_mb, 256)

    def test_parse_aliases(self):
        for alias in ("du", "archive"):
            ns = self._parser().parse_args([alias])
            self.assertIsNone(ns.storage_command, alias)

    # ---- parse args: subpath archive ----
    def test_parse_archive_subcommand(self):
        ns = self._parser().parse_args(
            ["storage", "archive", "myctf", "--git-remote",
             "https://git.example.com/me/archives.git", "--out", "/tmp/out"]
        )
        self.assertEqual(ns.storage_command, "archive")
        self.assertEqual(ns.workspace_name, "myctf")
        self.assertEqual(ns.git_remote,
                         "https://git.example.com/me/archives.git")
        self.assertEqual(ns.out, "/tmp/out")
        self.assertFalse(ns.yes)

    def test_parse_archive_yes_flag(self):
        ns = self._parser().parse_args(["du", "archive", "myctf", "-y"])
        self.assertTrue(ns.yes)

    # ---- handler: scan gọi StorageManager đúng tham số ----
    def test_handle_storage_scan_calls_manager_with_params(self):
        import contextlib
        import io
        import datetime as dt

        from ctf_downloader import cli_commands
        from ctf_downloader.services.storage_manager import (
            StorageManager,
            WorkspaceUsage,
        )

        fake_usage = WorkspaceUsage(
            name="ws1", path="/tmp/x/ws1", total_bytes=10,
            breakdown={"attachments": 0, "writeups": 0, "solvers": 0,
                       "misc": 10},
            largest_files=[], challenge_count=0, ended=None,
        )
        ns = Namespace(base_dir="/tmp/fake_ctf", threshold_mb=512,
                       storage_command=None)
        with patch.object(StorageManager, "scan_usage",
                          return_value=[fake_usage]) as m_scan, \
             patch.object(StorageManager, "format_report",
                          return_value="REPORT") as m_rep, \
             patch.object(StorageManager, "suggest_actions",
                          return_value=["✅ ok"]) as m_sug:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli_commands.handle_storage(ns)
        m_scan.assert_called_once_with("/tmp/fake_ctf")
        _, kwargs = m_rep.call_args
        self.assertEqual(kwargs.get("threshold_mb"), 512)
        self.assertEqual(m_rep.call_args.args[0], [fake_usage])
        # suggest có gợi ý thật → in; dòng ✅ all-good không in
        out = buf.getvalue()
        self.assertIn("REPORT", out)
        m_sug.assert_called_once_with("/tmp/fake_ctf", threshold_mb=512)

    def test_handle_storage_scan_no_meaningful_suggestion_hides_hint_block(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.services.storage_manager import StorageManager

        ns = Namespace(base_dir="/tmp/fake_ctf", threshold_mb=1024,
                       storage_command=None)
        with patch.object(StorageManager, "scan_usage", return_value=[]), \
             patch.object(StorageManager, "format_report",
                          return_value="(không có workspace nào)"), \
             patch.object(StorageManager, "suggest_actions",
                          return_value=["✅ Mọi thứ ổn."]):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli_commands.handle_storage(ns)
        self.assertNotIn("Gợi ý", buf.getvalue())

    # ---- handler: archive non-tty không --yes → exit 2, KHÔNG đụng dữ liệu ----
    def test_archive_non_tty_without_yes_exits_2(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.services.storage_manager import StorageManager

        ns = Namespace(base_dir="/tmp/fake_ctf", threshold_mb=1024,
                       storage_command="archive", workspace_name="myctf",
                       git_remote=None, out=None, yes=False)
        fake_tty = io.StringIO()
        fake_tty.isatty = lambda: False
        with patch.object(sys, "stdin", fake_tty), \
             patch.object(os.path, "isdir", return_value=True), \
             patch.object(StorageManager, "archive_workspace") as m_arch:
            with self.assertRaises(SystemExit) as cm:
                cli_commands.handle_storage(ns)
        self.assertEqual(cm.exception.code, 2)
        m_arch.assert_not_called()

    def test_archive_non_tty_with_yes_calls_manager_and_skips_delete(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.services.storage_manager import StorageManager

        ns = Namespace(
            base_dir="/tmp/fake_ctf", threshold_mb=1024,
            storage_command="archive", workspace_name="myctf",
            git_remote="https://git.example.com/r.git", out="/tmp/out",
            yes=True,
        )
        result = {"archive_path": "/tmp/out/myctf_20260824.tar.gz",
                  "original_bytes": 1000, "archived_bytes": 100, "ratio": 0.1}
        fake_tty = io.StringIO()
        fake_tty.isatty = lambda: False
        with patch.object(sys, "stdin", fake_tty), \
             patch.object(os.path, "isdir", return_value=True), \
             patch.object(StorageManager, "archive_workspace",
                          return_value=result) as m_arch, \
             patch.object(StorageManager, "delete_workspace") as m_del:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli_commands.handle_storage(ns)
        m_arch.assert_called_once_with(
            os.path.join("/tmp/fake_ctf", "myctf"),
            out_dir="/tmp/out",
            git_remote="https://git.example.com/r.git",
        )
        # non-tty: không bao giờ xoá workspace gốc
        m_del.assert_not_called()
        self.assertIn("myctf_20260824.tar.gz", buf.getvalue())
        self.assertIn("ratio", buf.getvalue())

    def test_archive_missing_workspace_exits_1(self):
        from ctf_downloader import cli_commands

        ns = Namespace(base_dir="/tmp/fake_ctf", threshold_mb=1024,
                       storage_command="archive", workspace_name="nope",
                       git_remote=None, out=None, yes=True)
        with patch.object(os.path, "isdir", return_value=False):
            with self.assertRaises(SystemExit) as cm:
                cli_commands.handle_storage(ns)
        self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
