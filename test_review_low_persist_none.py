"""Review advisory LOW (commit 536364d) — None từ update_global_config bị
bỏ qua ở hai đường còn lại.

``update_global_config`` → ``locked_update_json`` trả None khi thư mục chứa
global config BIẾN MẤT giữa chừng (không hồi sinh dir) hoặc mutator trả
SKIP_WRITE. Hai caller sau đây không nhìn tín hiệu None:

1. ``ctf config <key> <value>`` (cli_commands.handle_config): mutator
   ``_set_key`` luôn trả state nên None chỉ có thể là dir-gone — code cũ
   vẫn ``Logger.success("Đã lưu")`` + exit 0 dù KHÔNG persist gì. Fix theo
   pattern register đã sửa ở review c18-2: warning rõ + exit 1.
2. Menu ``_save_current_workspace`` (interactive_menu): mutator ``_mut``
   cũng luôn trả state — nhánh None cũ im lặng, khác hẳn nhánh OSError có
   warning. Fix: log warning cùng mức, không refresh cache từ None.

TDD red→green. Mọi I/O trong tmpdir; không mạng.
Chạy: python3 -m pytest test_review_low_persist_none.py -q
"""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _dir_gone_updater(mutator):
    """Mô phỏng locked_update_json khi thư mục chứa config đã biến mất:
    mutator chạy bình thường (KHÔNG SKIP_WRITE) nhưng kết quả vẫn None."""
    mutator({})
    return None


class TestConfigSetModeNone(unittest.TestCase):
    """handle_config set-mode: updater trả None → exit 1, không success."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rev_none_cfg_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

        from ctf_downloader.storage import global_config as gc
        self.gc = gc
        cfg_dir = os.path.join(self._tmp, "cfg")
        old = (gc.CONFIG_DIR, gc.GLOBAL_CONFIG_FILE)
        gc.CONFIG_DIR = cfg_dir
        gc.GLOBAL_CONFIG_FILE = os.path.join(cfg_dir, "config.json")
        self.cfg_file = gc.GLOBAL_CONFIG_FILE
        self.addCleanup(
            lambda: setattr(gc, "CONFIG_DIR", old[0]))
        self.addCleanup(
            lambda: setattr(gc, "GLOBAL_CONFIG_FILE", old[1]))

        orig_update = gc.update_global_config
        gc.update_global_config = _dir_gone_updater
        self.addCleanup(setattr, gc, "update_global_config", orig_update)

        from ctf_downloader.utils.logger import Logger
        self.successes, self.warns = [], []
        orig_success, orig_warning = Logger.success, Logger.warning
        Logger.success = staticmethod(
            lambda msg, **kw: self.successes.append(str(msg)))
        Logger.warning = staticmethod(
            lambda msg, **kw: self.warns.append(str(msg)))
        self.addCleanup(setattr, Logger, "success", orig_success)
        self.addCleanup(setattr, Logger, "warning", orig_warning)

    def test_set_returns_none_exits_1_without_success_log(self):
        from ctf_downloader.cli import build_unified_parser
        from ctf_downloader.cli_commands import handle_config

        ns = build_unified_parser().parse_args(["config", "auto-sync", "off"])
        with self.assertRaises(SystemExit) as ei:
            handle_config(ns)                      # phải exit 1, không success

        self.assertEqual(1, ei.exception.code,
                         "không persist gì thì exit-code phải phản ánh thất bại")
        self.assertFalse(self.successes,
                         f"không được báo 'Đã lưu' khi updater trả None: "
                         f"{self.successes}")
        self.assertTrue(self.warns,
                        "thất bại persist phải được log warning rõ")


class TestMenuSaveCurrentWorkspaceNone(unittest.TestCase):
    """Menu _save_current_workspace: updater trả None → warning, cache giữ."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rev_none_menu_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

        import ctf_downloader.interactive_menu as im_mod
        orig_update = im_mod.update_global_config
        im_mod.update_global_config = _dir_gone_updater
        self.addCleanup(setattr, im_mod, "update_global_config", orig_update)

        from ctf_downloader.utils.logger import Logger
        self.warns = []
        orig_warning = Logger.warning
        Logger.warning = staticmethod(
            lambda msg, **kw: self.warns.append(str(msg)))
        self.addCleanup(setattr, Logger, "warning", orig_warning)

    def _make_menu(self, ws_dir):
        from ctf_downloader.interactive_menu import CTFInteractiveConsole
        menu = CTFInteractiveConsole.__new__(CTFInteractiveConsole)
        menu.config = {"workspaces": {}, "default_workspace": None,
                       "auth": {}}
        menu.workspace_path = ws_dir
        menu.cookie = "CK_NEW"
        menu.token = None
        return menu

    def test_updater_returns_none_logs_warning_like_oserror_branch(self):
        ws_dir = os.path.join(self._tmp, "wsA")
        os.makedirs(ws_dir)
        menu = self._make_menu(ws_dir)

        menu._save_current_workspace()             # không raise, không im lặng

        self.assertTrue(self.warns,
                        "nhánh None phải log warning cùng mức nhánh OSError")
        snapshot = {"workspaces": {}, "default_workspace": None, "auth": {}}
        self.assertEqual(snapshot, menu.config,
                         "cache nội bộ không được refresh từ None")


if __name__ == "__main__":
    unittest.main()
