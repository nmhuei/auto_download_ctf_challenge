"""Phase 3 tests: services.session_factory + services.auth_service + storage.global_config.

Toàn bộ test Phase 2 (storage.fileio + storage.constants) đã được chuyển nguyên văn
sang file này theo kế hoạch rebuild kiến trúc.
"""
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import mock

from ctf_downloader.storage.fileio import atomic_write_json, atomic_write_text, locked_update_json
from ctf_downloader.storage import constants


# ---------------------------------------------------------------------------
# Phase 2 (chuyển nguyên văn từ test_arch_phase2.py)
# ---------------------------------------------------------------------------

class TestFileIO(unittest.TestCase):
    def test_atomic_roundtrip_and_corrupt_backup(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "x.json"
            atomic_write_json(p, {"a": 1})
            self.assertEqual(json.loads(p.read_text()), {"a": 1})
            p.write_text("{corrupt")            # hỏng
            out = locked_update_json(p, lambda d: {**(d or {}), "b": 2})
            self.assertEqual(out, {"b": 2})
            self.assertTrue((pathlib.Path(d) / "x.json.bak").exists())

    def test_atomic_write_text(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "sub" / "note.txt"
            p.parent.mkdir()
            atomic_write_text(p, "hello")
            self.assertEqual(p.read_text(encoding="utf-8"), "hello")
            # no leftover tmp files
            self.assertEqual(list(p.parent.glob("*.tmp")), [])

    def test_locked_update_normal_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "state.json"
            atomic_write_json(p, {"n": 1})
            out = locked_update_json(p, lambda d: {**d, "n": d["n"] + 1})
            self.assertEqual(out, {"n": 2})
            self.assertEqual(json.loads(p.read_text()), {"n": 2})

    def test_locked_update_mutator_returns_none_keeps_file(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "state.json"
            atomic_write_json(p, {"keep": True})
            out = locked_update_json(p, lambda d: None)
            self.assertEqual(out, {"keep": True})


class TestConstants(unittest.TestCase):
    def test_solved_markers(self):
        self.assertEqual(constants.SOLVED_DONE, "- [x] Solved")
        self.assertEqual(constants.SOLVED_TODO, "- [ ] Solved")
        self.assertIsInstance(constants.SOLVED_MARKERS_DONE, tuple)
        self.assertIn("- [x] Solved", constants.SOLVED_MARKERS_DONE)
        self.assertIn("✅ Solved", constants.SOLVED_MARKERS_DONE)
        self.assertIn("Status: ✅", constants.SOLVED_MARKERS_DONE)

    def test_format_constants(self):
        self.assertIn("{info}", constants.TARGET_CONNECTION_FMT)
        self.assertIn("{total_files}", constants.SUMMARY_FILES_LINE)
        self.assertTrue(constants.LIVE_RANK_PREFIX.startswith("- **Live Rank**"))

    def test_scalar_constants(self):
        self.assertEqual(constants.SOLVE_VAR_NAMES, ("HOST", "PORT", "TARGET_URL"))
        self.assertEqual(constants.FLAG_PLACEHOLDER, "FLAG{...}")
        self.assertEqual(constants.DEFAULT_CATEGORY, "Misc")


# ---------------------------------------------------------------------------
# Phase 3 mới
# ---------------------------------------------------------------------------

class TestGlobalConfigMove(unittest.TestCase):
    def test_interactive_menu_reexports_from_storage(self):
        from ctf_downloader import interactive_menu
        from ctf_downloader.storage import global_config
        self.assertIs(interactive_menu.load_global_config, global_config.load_global_config)
        self.assertIs(interactive_menu.save_global_config, global_config.save_global_config)

    def test_load_default_when_file_missing(self):
        from ctf_downloader.storage import global_config as gc
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(gc, "GLOBAL_CONFIG_FILE", os.path.join(d, "config.json")):
                cfg = gc.load_global_config()
        self.assertEqual(cfg, {
            "workspaces": {},
            "default_workspace": None,
            "workspace_root": gc.DEFAULT_WORKSPACE_ROOT,
            "auth": {},
        })

    def test_save_then_load_roundtrip(self):
        from ctf_downloader.storage import global_config as gc
        with tempfile.TemporaryDirectory() as d:
            fake = os.path.join(d, "config.json")
            with mock.patch.object(gc, "GLOBAL_CONFIG_FILE", fake), \
                 mock.patch.object(gc, "CONFIG_DIR", d):
                gc.save_global_config({"auth": {"/ws": {"cookie": "c", "token": "t"}}})
                cfg = gc.load_global_config()
        self.assertEqual(cfg["auth"]["/ws"]["token"], "t")


class TestAuthService(unittest.TestCase):
    def _fake_config_file(self, d, cfg):
        """Ghi config.json giả lập và trỏ global_config sang nó."""
        from ctf_downloader.storage import global_config as gc
        path = os.path.join(d, "config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        return mock.patch.object(gc, "GLOBAL_CONFIG_FILE", path)

    def test_cli_args_take_priority_over_saved_auth(self):
        from ctf_downloader.services.auth_service import AuthService
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.abspath(os.path.join(d, "ws"))
            with self._fake_config_file(d, {"auth": {ws: {"cookie": "saved_c", "token": "saved_t"}}}):
                result = AuthService.resolve(ws, cookie_arg="arg_c", token_arg="arg_t")
        self.assertEqual(result, ("arg_c", "arg_t"))

    def test_reads_auth_map_from_global_config(self):
        from ctf_downloader.services.auth_service import AuthService
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.abspath(os.path.join(d, "ctf_x"))
            with self._fake_config_file(d, {"auth": {ws: {"cookie": "session=abc", "token": "tok"}}}):
                self.assertEqual(AuthService.resolve(ws), ("session=abc", "tok"))
                # token_arg CLI đè token đã lưu nhưng giữ cookie đã lưu
                self.assertEqual(AuthService.resolve(ws, token_arg="new_t"), ("session=abc", "new_t"))

    def test_unknown_workspace_returns_none_cookie(self):
        from ctf_downloader.services.auth_service import AuthService
        with tempfile.TemporaryDirectory() as d:
            with self._fake_config_file(d, {"auth": {}}):
                result = AuthService.resolve(os.path.join(d, "nowhere"), token_arg="tk")
        self.assertEqual(result, (None, "tk"))

    def test_cookie_arg_as_file_path_is_read(self):
        from ctf_downloader.services.auth_service import AuthService
        with tempfile.TemporaryDirectory() as d:
            cf = pathlib.Path(d) / "cookie.txt"
            cf.write_text("session=file_cookie\n", encoding="utf-8")
            result = AuthService.resolve(str(d), cookie_arg=str(cf))
        self.assertEqual(result, ("session=file_cookie", None))

    def test_cli_get_auth_for_workspace_matches_auth_service(self):
        from ctf_downloader.cli import get_auth_for_workspace
        from ctf_downloader.services.auth_service import AuthService
        with tempfile.TemporaryDirectory() as d:
            ws = os.path.abspath(os.path.join(d, "ctf_y"))
            cases = [
                (ws, None, None),
                (ws, "raw_cookie", None),
                (ws, "raw_cookie", "tok"),
                (os.path.join(d, "unknown-ws"), None, "only_tok"),
            ]
            with self._fake_config_file(d, {"auth": {ws: {"cookie": "sc", "token": "st"}}}):
                for wspath, ck, tk in cases:
                    self.assertEqual(
                        get_auth_for_workspace(wspath, ck, tk),
                        AuthService.resolve(wspath, ck, tk),
                    )


class TestSessionFactory(unittest.TestCase):
    def test_create_session_sets_cookies_and_auth_header(self):
        from ctf_downloader.services.session_factory import create_session
        s = create_session(cookie="session=abc; other=1", token="mytoken")
        self.assertEqual(s.cookies.get("session"), "abc")
        self.assertEqual(s.cookies.get("other"), "1")
        self.assertTrue(s.headers.get("Authorization"))

    def test_create_session_custom_headers_and_timeout_passthrough(self):
        from ctf_downloader.services.session_factory import create_session
        s = create_session(custom_headers={"X-Probe": "1"}, timeout=7)
        self.assertEqual(s.headers.get("X-Probe"), "1")

    def test_thread_local_sessions_gives_distinct_copy_per_thread(self):
        from ctf_downloader.services.session_factory import create_session, thread_local_sessions
        master = create_session(cookie="shared=yes", custom_headers={"X-Master": "h"})
        got = {}
        barrier = threading.Barrier(3)

        def worker(i):
            barrier.wait()
            got[i] = sess_holder["get"]()

        with thread_local_sessions(master) as get_sess:
            sess_holder = {"get": get_sess}
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        self.assertEqual(len({id(s) for s in got.values()}), 3)      # mỗi thread 1 session riêng
        self.assertNotIn(id(master), {id(s) for s in got.values()})  # không dùng lại master
        for s in got.values():                                       # copy đủ cookies + headers
            self.assertEqual(s.cookies.get("shared"), "yes")
            self.assertEqual(s.headers.get("X-Master"), "h")

    def test_thread_local_sessions_same_thread_reuses_session(self):
        from ctf_downloader.services.session_factory import thread_local_sessions, create_session
        master = create_session()
        with thread_local_sessions(master) as get_sess:
            self.assertIs(get_sess(), get_sess())


if __name__ == "__main__":
    unittest.main()
