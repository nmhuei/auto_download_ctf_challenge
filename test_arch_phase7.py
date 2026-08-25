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
import shutil
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
        # Đổi CÓ CHỦ ĐÍCH (fix finding codex 05_help): `ctf --help` giờ là
        # HelpScreen PHOSPHOR FIELD KIT §4.8 thay vì usage/options argparse.
        ("main.py", "--help"): "bộ kit tác chiến capture-the-flag",
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
        # Fix pre-existing (không liên quan UI redesign): __init__.py đã bump
        # lên 3.0.0 từ trước nhưng assertion này còn kẹt 2.0.0.
        self.assertIn("ctf-toolkit 3.0.0", r.stdout + r.stderr)


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


class TestNewServiceCommands(unittest.TestCase):
    """Phase 7c — wire 5 lệnh mới: sync / export-pack / history / sniper /
    serve. Parse args + mock service assert đúng tham số + exit code
    thiếu workspace = 1."""

    MISSING_WS = "/nonexistent_ws_phase7"

    def _parser(self):
        from ctf_downloader.cli import build_unified_parser

        return build_unified_parser()

    # ---- parse args ----
    def test_parse_sync_defaults_and_verify(self):
        ns = self._parser().parse_args(["sync"])
        self.assertEqual(ns.workspace, ".")
        self.assertFalse(ns.verify)
        ns = self._parser().parse_args(["sync", "-w", "ws", "--verify"])
        self.assertEqual(ns.workspace, "ws")
        self.assertTrue(ns.verify)

    def test_parse_export_pack(self):
        ns = self._parser().parse_args(
            ["export-pack", "-w", "ws", "--out", "/tmp/out"])
        self.assertEqual(ns.workspace, "ws")
        self.assertEqual(ns.out, "/tmp/out")

    def test_parse_history_redacted_by_default(self):
        ns = self._parser().parse_args(["history", "-w", "ws"])
        self.assertFalse(ns.show_all)
        ns = self._parser().parse_args(["history", "-w", "ws", "--all"])
        self.assertTrue(ns.show_all)

    def test_parse_sniper_defaults_and_flags(self):
        ns = self._parser().parse_args(["sniper"])
        self.assertEqual(ns.workspace, ".")
        self.assertIsNone(ns.start_at)
        self.assertFalse(ns.retry_wrong)
        self.assertEqual(ns.poll, 10)
        ns = self._parser().parse_args(
            ["sniper", "-w", "ws", "--start-at", "2026-08-30T00:00:00Z",
             "--retry-wrong", "--poll", "5"])
        self.assertEqual(ns.workspace, "ws")
        self.assertEqual(ns.start_at, "2026-08-30T00:00:00Z")
        self.assertTrue(ns.retry_wrong)
        self.assertEqual(ns.poll, 5)

    def test_parse_serve_default_port(self):
        ns = self._parser().parse_args(["serve"])
        self.assertEqual(ns.workspace, ".")
        self.assertEqual(ns.port, 8689)
        ns = self._parser().parse_args(["serve", "-w", "ws", "--port", "9999"])
        self.assertEqual(ns.workspace, "ws")
        self.assertEqual(ns.port, 9999)

    def test_sync_alias_now_points_to_sync_command_not_watch(self):
        # Alias 'sync' chuyển từ watch sang lệnh sync metadata 2 chiều (P2-1).
        ns = self._parser().parse_args(["sync"])
        self.assertEqual(ns.subcommand, "sync")

    # ---- handler: mock service assert đúng tham số ----
    def test_handle_sync_calls_pull_service_with_resolved_platform(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.services.platform_resolver import PlatformResolver
        from ctf_downloader.services.pull_service import PullService

        platform = object()
        ns = Namespace(workspace="somews", verify=False)
        with patch.object(cli_commands, "get_auth_for_workspace",
                          return_value=(None, None)) as m_auth, \
             patch.object(PlatformResolver, "for_workspace",
                          return_value=(None, platform, None)) as m_resolve, \
             patch.object(PullService, "sync_workspace",
                          return_value={"ok": True}) as m_sync:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli_commands.handle_sync(ns)
        m_auth.assert_called_once_with("somews")
        _, kwargs = m_resolve.call_args
        self.assertIsNone(kwargs.get("cookie"))
        m_sync.assert_called_once()
        self.assertIs(m_sync.call_args.args[1], platform)

    def test_handle_sync_verify_runs_verify(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.services.platform_resolver import PlatformResolver
        from ctf_downloader.services.pull_service import PullService

        ns = Namespace(workspace="somews", verify=True)
        with patch.object(cli_commands, "get_auth_for_workspace",
                          return_value=(None, None)), \
             patch.object(PlatformResolver, "for_workspace",
                          return_value=(None, object(), None)), \
             patch.object(PullService, "sync_workspace",
                          return_value={"ok": True}), \
             patch.object(PullService, "verify",
                          return_value={"unsolved_locally_solved_remotely":
                                        []}) as m_verify:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli_commands.handle_sync(ns)
        m_verify.assert_called_once()

    def test_handle_sync_failure_exits_1(self):
        from ctf_downloader import cli_commands
        from ctf_downloader.services.platform_resolver import PlatformResolver
        from ctf_downloader.services.pull_service import PullService

        ns = Namespace(workspace="somews", verify=False)
        with patch.object(cli_commands, "get_auth_for_workspace",
                          return_value=(None, None)), \
             patch.object(PlatformResolver, "for_workspace",
                          side_effect=ValueError("no url")), \
             patch.object(PullService, "sync_workspace") as m_sync:
            with self.assertRaises(SystemExit) as cm:
                cli_commands.handle_sync(ns)
        self.assertEqual(cm.exception.code, 1)
        m_sync.assert_not_called()

    def test_handle_export_pack_warns_then_builds_zip_path(self):
        import contextlib
        import io
        from pathlib import Path

        from ctf_downloader import cli_commands
        from ctf_downloader.services.writeup_exporter import WriteupExporter

        pack = Path("/tmp/out/ws_writeup_20260824")
        ns = Namespace(workspace="ws", out="/tmp/out")
        with patch.object(WriteupExporter, "collect",
                          return_value=[object()]), \
             patch.object(WriteupExporter, "validate",
                          return_value=["⚠️ [web] chall: thiếu flag"]) as m_val, \
             patch.object(WriteupExporter, "build_pack",
                          return_value=pack) as m_pack:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli_commands.handle_export_pack(ns)
        m_val.assert_called_once()
        m_pack.assert_called_once_with("/tmp/out")
        out = buf.getvalue()
        self.assertIn("⚠️ [web] chall: thiếu flag", out)
        self.assertIn(str(pack) + ".zip", out)

    def test_handle_export_pack_no_entries_exits_1(self):
        from ctf_downloader import cli_commands
        from ctf_downloader.services.writeup_exporter import WriteupExporter

        ns = Namespace(workspace="ws", out=None)
        with patch.object(WriteupExporter, "collect", return_value=[]), \
             patch.object(WriteupExporter, "validate", return_value=[]), \
             patch.object(WriteupExporter, "build_pack",
                          side_effect=ValueError("trống")):
            with self.assertRaises(SystemExit) as cm:
                cli_commands.handle_export_pack(ns)
        self.assertEqual(cm.exception.code, 1)

    def test_handle_history_renders_table_with_redacted_flag(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.storage.workspace_repo import WorkspaceRepo

        hist = {"entries": [
            {"flag": "PTITCTF{sup3r_secret}", "challenge_id": 12,
             "result": "correct", "timestamp": "2026-08-24T09:00:00Z"},
            {"flag": "FLAG{dead}", "challenge_id": "warmup",
             "result": "incorrect", "timestamp": "2026-08-24T09:05:00Z"},
        ]}
        ns = Namespace(workspace=".", show_all=False)
        with patch.object(WorkspaceRepo, "load_submit_history",
                          return_value=hist), \
             patch.object(WorkspaceRepo, "find_challenge",
                          return_value={"name": "Chall A"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli_commands.handle_history(ns)
        out = buf.getvalue()
        self.assertIn("Chall A", out)
        self.assertIn("🚩✔", out)
        self.assertIn("⛔", out)
        self.assertIn("PTIT***", out)
        self.assertNotIn("sup3r_secret", out)

    def test_handle_history_all_reveals_flag(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.storage.workspace_repo import WorkspaceRepo

        hist = {"entries": [
            {"flag": "PTITCTF{sup3r_secret}", "challenge_id": 12,
             "result": "correct", "timestamp": "2026-08-24T09:00:00Z"},
        ]}
        ns = Namespace(workspace=".", show_all=True)
        with patch.object(WorkspaceRepo, "load_submit_history",
                          return_value=hist), \
             patch.object(WorkspaceRepo, "find_challenge",
                          return_value={"name": "Chall A"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli_commands.handle_history(ns)
        self.assertIn("PTITCTF{sup3r_secret}", buf.getvalue())

    def test_handle_history_empty_is_graceful_no_exit(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.storage.workspace_repo import WorkspaceRepo

        ns = Namespace(workspace=".", show_all=False)
        with patch.object(WorkspaceRepo, "load_submit_history",
                          return_value={"entries": []}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli_commands.handle_history(ns)  # KHÔNG SystemExit
        self.assertIn("Chưa có lịch sử submit", buf.getvalue())

    def test_handle_sniper_calls_service_run_with_flags(self):
        from ctf_downloader import cli_commands
        from ctf_downloader.services.sniper_service import SniperService
        from ctf_downloader.services.submit_service import SubmitService

        ns = Namespace(workspace="ws", start_at="2026-08-30T00:00:00Z",
                       retry_wrong=True, poll=5)
        with patch.object(cli_commands, "get_auth_for_workspace",
                          return_value=("ck", "tk")), \
             patch.object(SubmitService, "__init__",
                          return_value=None) as m_init, \
             patch.object(SniperService, "run", return_value={}) as m_run:
            cli_commands.handle_sniper(ns)
        _, kwargs = m_init.call_args
        self.assertEqual(kwargs.get("workspace_dir"), "ws")
        self.assertEqual(kwargs.get("cookie"), "ck")
        run_kwargs = m_run.call_args.kwargs
        self.assertEqual(run_kwargs.get("poll_interval"), 5.0)
        self.assertEqual(run_kwargs.get("start_at"), "2026-08-30T00:00:00Z")
        self.assertTrue(run_kwargs.get("retry_wrong"))

    def test_handle_sniper_submit_init_error_exits_1(self):
        from ctf_downloader import cli_commands
        from ctf_downloader.services.sniper_service import SniperService
        from ctf_downloader.services.submit_service import SubmitService

        ns = Namespace(workspace="ws", start_at=None, retry_wrong=False,
                       poll=10)
        with patch.object(cli_commands, "get_auth_for_workspace",
                          return_value=(None, None)), \
             patch.object(SubmitService, "__init__",
                          side_effect=ValueError("no url")), \
             patch.object(SniperService, "run") as m_run:
            with self.assertRaises(SystemExit) as cm:
                cli_commands.handle_sniper(ns)
        self.assertEqual(cm.exception.code, 1)
        m_run.assert_not_called()

    def test_handle_serve_starts_dashboard_with_port(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.services.web_dashboard import WebDashboard

        ns = Namespace(workspace=".", port=8123)
        with patch.object(WebDashboard, "serve") as m_serve:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli_commands.handle_serve(ns)
        m_serve.assert_called_once_with(port=8123)
        self.assertIn("http://127.0.0.1:8123/", buf.getvalue())

    # ---- exit code thiếu workspace = 1 (end-to-end qua main.py) ----
    def test_new_commands_missing_workspace_exit_1(self):
        for argv in (["sync"], ["export-pack"], ["history"], ["sniper"],
                     ["serve"]):
            with self.subTest(cmd=argv[0]):
                r = _run(["main.py"] + argv +
                         ["-w", self.MISSING_WS])
                self.assertEqual(r.returncode, 1,
                                 f"{argv}: {r.stdout + r.stderr}")


class TestPhosphorHelpScreen(unittest.TestCase):
    """Fix finding codex 05_help: `ctf --help` = HelpScreen spec §4.8
    (banner B amber + tagline + CÚ PHÁP + LỆNH pad-12 + FooterBar), không
    emoji chrome, không liệt kê alias trong bảng chính."""

    @classmethod
    def setUpClass(cls):
        cls.out = _run(["main.py", "--help"]).stdout

    def test_spec_markers_present(self):
        for marker in ("CÚ PHÁP", "LỆNH",
                       "bộ kit tác chiến capture-the-flag",
                       "ctf <lệnh> [tuỳ chọn]",
                       "q thoát"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.out)

    def test_no_emoji_chrome(self):
        # Glyph rule: emoji chrome bị cấm (⚡📝🏷️👀🩺💾🔄📦📜🎯🌐).
        banned = "⚡📝🏷👀🩺💾🔄📦📜🎯🌐"
        hits = [ch for ch in banned if ch in self.out]
        self.assertEqual([], hits)

    def test_argparse_chrome_gone(self):
        # Không còn usage/options argparse lẫn khối aliases "{a,b,c}".
        self.assertNotIn("usage:", self.out)
        self.assertNotIn("options:", self.out)
        self.assertNotIn("{pull,download,clone,", self.out)

    def test_command_column_pad12_one_line_each(self):
        import re
        for name in ("pull", "sync", "status", "history", "menu"):
            # "  " indent + tên ljust(12) → pull + 8 spaces trước mô tả;
            # export-pack (11 ký tự) vẫn vừa cột cố định.
            row = re.search(rf"(?m)^  {name} +\S", self.out)
            self.assertIsNotNone(row, name)
        self.assertRegex(self.out, r"(?m)^  pull {8}Tải đề")
        self.assertRegex(self.out, r"(?m)^  export-pack Đóng gói")

    def test_aliases_not_listed_in_command_table(self):
        # Alias KHÔNG xuất hiện ở cột lệnh (dạng "  download  ...").
        import re
        for alias in ("download", "clone", "scan", "du", "log"):
            self.assertIsNone(
                re.search(rf"(?m)^  {alias} +\S", self.out),
                f"alias '{alias}' không được nằm trong bảng LỆNH")


class TestAppHeaderFooterFrame(unittest.TestCase):
    """Fix finding codex 01_status: lệnh thường bọc AppHeader đầu + FooterBar
    cuối (spec §4.1/§4.7). Non-TTY → plain text không ANSI."""

    def _frame_output(self):
        import contextlib
        import io

        from ctf_downloader import cli

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._run_framed(lambda a: print("BODY"), Namespace(workspace="wsA"),
                            "status")
        return buf.getvalue()

    def test_header_before_body_footer_after(self):
        out = self._frame_output()
        self.assertIn("CTF·TOOLKIT", out)
        self.assertIn("status · wsA", out)
        self.assertIn("BODY", out)
        self.assertLess(out.index("CTF·TOOLKIT"), out.index("BODY"))
        self.assertGreater(out.index("q thoát"), out.index("BODY"))

    def test_footer_bindings_standard_set(self):
        out = self._frame_output()
        for frag in ("↑↓ di chuyển", "? help", "q thoát", " · "):
            self.assertIn(frag, out)

    def test_framed_commands_wired_in_dispatch(self):
        import inspect
        import re

        from ctf_downloader import cli

        src = inspect.getsource(cli.main)
        for label in ("'status'", "'workspaces'", "'storage'", "'sync'",
                      "'export-pack'", "'history'"):
            self.assertRegex(
                src, rf"_run_framed\([^\n]*{re.escape(label)}")


class TestShellCompletions(unittest.TestCase):
    """Phase 7 — shell completion cho CLI `ctf` (bash/zsh).

    Kiểm tra cú pháp (bash -n / zsh -n) và nội dung: subcommand + flag
    chính khớp với argparse trong ctf_downloader/cli.py.
    """

    COMPLETIONS_DIR = os.path.join(ROOT, "completions")

    def _path(self, name):
        return os.path.join(self.COMPLETIONS_DIR, name)

    def test_bash_completion_syntax_ok(self):
        path = self._path("ctf.bash")
        self.assertTrue(os.path.isfile(path), "thiếu completions/ctf.bash")
        proc = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_zsh_completion_syntax_ok(self):
        path = self._path("ctf.zsh")
        if not shutil.which("zsh"):
            self.skipTest("zsh không có trên PATH")
        proc = subprocess.run(["zsh", "-n", path], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_completions_cover_subcommands(self):
        from ctf_downloader.cli import build_unified_parser

        parser = build_unified_parser()
        subs = set(parser._subparsers._group_actions[0].choices)
        for fname in ("ctf.bash", "ctf.zsh"):
            with open(self._path(fname), encoding="utf-8") as f:
                body = f.read()
            for cmd in subs:
                self.assertIn(cmd, body, f"{fname} thiếu subcommand '{cmd}'")
            # flag chính của parser gốc
            for flag in ("--version", "--interactive", "--workspace"):
                self.assertIn(flag, body, f"{fname} thiếu flag '{flag}'")


class TestOpenCommand(unittest.TestCase):
    """Phase 7d — ``ctf open <challenge> [-w WS]``: mở thư mục challenge qua
    xdg-open (không shell=True, check=True). Parse args + mock subprocess
    assert đúng path + resolve fail -> exit 1."""

    MISSING_WS = "/nonexistent_ws_phase7"

    def _parser(self):
        from ctf_downloader.cli import build_unified_parser

        return build_unified_parser()

    # ---- parse args ----
    def test_parse_open_defaults(self):
        ns = self._parser().parse_args(["open", "baby-web"])
        self.assertEqual(ns.target, "baby-web")
        self.assertEqual(ns.workspace, ".")

    def test_parse_open_custom_workspace(self):
        ns = self._parser().parse_args(["open", "baby-web", "-w", "wsA"])
        self.assertEqual(ns.target, "baby-web")
        self.assertEqual(ns.workspace, "wsA")

    # ---- handler: mock resolve + subprocess assert đúng path ----
    def test_handle_open_runs_xdg_open_on_challenge_dir(self):
        import contextlib
        import io
        from pathlib import Path

        from ctf_downloader import cli_commands

        ns = Namespace(workspace="wsA", target="baby-web")
        meta_path = Path("/ws/wsA/challs/baby-web/metadata.json")
        with patch.object(cli_commands.StatusService, "resolve_challenge",
                          return_value=(meta_path, {"id": 1, "name": "Baby Web"})
                          ) as m_resolve, \
             patch.object(cli_commands.subprocess, "run") as m_run:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli_commands.handle_open(ns)
        m_resolve.assert_called_once()
        self.assertEqual(m_resolve.call_args.args[1], "baby-web")
        m_run.assert_called_once_with(
            ["xdg-open", "/ws/wsA/challs/baby-web"], check=True, shell=False)
        self.assertIn("/ws/wsA/challs/baby-web", buf.getvalue())

    def test_handle_open_missing_xdg_open_hints_and_exits_1(self):
        from ctf_downloader import cli_commands

        ns = Namespace(workspace="wsA", target="x")
        with patch.object(cli_commands.StatusService, "resolve_challenge",
                          return_value=("/ws/x/metadata.json",
                                        {"id": 2, "name": "X"})), \
             patch.object(cli_commands.subprocess, "run",
                          side_effect=FileNotFoundError("xdg-open")):
            with self.assertRaises(SystemExit) as cm:
                cli_commands.handle_open(ns)
        self.assertEqual(cm.exception.code, 1)

    # ---- resolve fail / thiếu workspace -> exit 1 ----
    def test_handle_open_resolve_fail_exits_1(self):
        import contextlib
        import io

        from ctf_downloader import cli_commands
        from ctf_downloader.services.status_service import ChallengeNotFoundError

        ns = Namespace(workspace=self.MISSING_WS, target="ghost")
        with patch.object(cli_commands.StatusService, "resolve_challenge",
                          side_effect=ChallengeNotFoundError(
                              "Challenge not found: 'ghost'")):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as cm:
                    cli_commands.handle_open(ns)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Challenge not found", buf.getvalue())

    def test_open_missing_workspace_exit_1_end_to_end(self):
        r = _run(["main.py", "open", "ghost", "-w", self.MISSING_WS])
        self.assertEqual(r.returncode, 1,
                         f"open: {r.stdout + r.stderr}")


if __name__ == "__main__":
    unittest.main()
