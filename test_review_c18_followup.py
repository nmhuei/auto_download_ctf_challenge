"""Follow-up review commit c5c8044 — 5 finding MỚI (vòng c18-2):

1. [MED] rank_service._render_scoreboard (panel TERMINAL): ``pos`` từ JSON
   server vào Rich NGUYÊN — OSC/CSI injection đổi title/màu terminal
   (title/name/my_rank/total_teams đã strip từ C14/c18, pos bị sót).
2. [MED] hai đường ghi global config còn load-stale-save — ``ctf config``
   và menu ``_save_current_workspace`` (self.config giữ từ lúc mở menu,
   lưu lại đè mất register_state/auth tiến trình khác ghi giữa chừng).
   Fix: chuyển sang ``update_global_config(mutator)`` — đọc-mutate-ghi
   TRONG CÙNG khóa flock.
3. [LOW] register_service._commit_attempt: updater trả None cho cả
   SKIP_WRITE (thua cuộc) lẫn "thư mục config biến mất" — caller chỉ nhìn
   preempted["wait"] nên case sau vẫn báo thành công.
4. [LOW] OSError (PermissionError...) từ global_config lan qua run() che
   exit-code mapping VÀ chặn in/lưu credentials dù account ĐÃ tạo phía
   server.
5. [LOW] md_cell không xử lý backtick — tên team chứa `` ` `` đóng
   code-span sớm, vỡ dòng bullet RANKING.md / badge SUMMARY.md.

TDD: các test RED tái hiện bug TRƯỚC khi sửa. Mọi network bị mock.
Chạy: python3 -m pytest test_review_c18_followup.py -q
"""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.services.rank_service import RankService
from ctf_downloader.services.register_service import (
    COMMIT_OK,
    COMMIT_PREEMPTED,
    COMMIT_UNPERSISTED,
    RegisterService,
)
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def render_panel(panel, width=100):
    """Render một rich renderable ra text thuần (như test_hunter_c14) —
    no_color để ESC còn sót trong output chắc chắn là ESC do DỮ LIỆU mang
    vào, không phải style."""
    buf = io.StringIO()
    from rich.console import Console
    from ctf_downloader.ui.theme import load_theme
    Console(file=buf, theme=load_theme(None), width=width, no_color=True,
            highlight=False).print(panel)
    return buf.getvalue()


def _patch_global_cfg(tmp, cfg_dir_name="cfg"):
    """Trỏ global config về tmp (trả về (gc_module, cfg_file_path))."""
    from ctf_downloader.storage import global_config as gc
    cfg_dir = Path(tmp) / cfg_dir_name
    cfg_file = cfg_dir / "config.json"
    old = (gc.CONFIG_DIR, gc.GLOBAL_CONFIG_FILE)

    def _restore():
        gc.CONFIG_DIR, gc.GLOBAL_CONFIG_FILE = old

    gc.CONFIG_DIR, gc.GLOBAL_CONFIG_FILE = str(cfg_dir), str(cfg_file)
    return gc, cfg_file, _restore


_URL = "https://gz.example.com"


class _OkPlatform:
    def __init__(self):
        self.calls = 0

    def register(self, *, username, email, password, verify_email_hook=None):
        self.calls += 1
        return {"ok": True, "message": "Registered"}


class _FakeInfo:
    platform_type = "gzctf"
    confidence = "high"


# ======================================================================
# FINDING 1 — panel terminal: pos (và mọi trường gốc server) phải strip
# ======================================================================
class TestF1TerminalPosInjection(unittest.TestCase):
    def test_f1_pos_osc_csi_not_passed_through(self):
        # RED (MED): pos do server trả chứa OSC (đổi title terminal) + CSI
        # (đổi màu) đi NGUYÊN vào Rich Table — đúng kịch bản
        # ``pos="\x1b]0;pwned\x07\x1b[31m9"`` của review.
        data = {"title": "CTF", "standings": [
            {"pos": "\x1b]0;pwned\x07\x1b[31m9", "name": "A", "score": 5},
        ]}
        out = render_panel(RankService._render_scoreboard(data))
        self.assertNotIn("\x1b", out,
                         "ESC/OSC từ pos server phải được strip trước khi "
                         "render panel terminal")
        self.assertIn("9", out)          # vị trí vẫn hiển thị sau khi strip

    def test_f1_hostile_row_no_escape_survives_anywhere(self):
        # Belt & braces: MỌI trường gốc server trong panel hostile -> output
        # terminal không còn byte ESC nào.
        data = {
            "title": "T\x1b[31mITLE",
            "my_team": "\x1b[1mME",
            "my_user": "\x1b]0;u\x07me",
            "my_rank": "\x1b]0;r\x073",
            "total_teams": "\x1b[2m10",
            "standings": [
                {"pos": "\x1b]0;p\x071st",
                 "name": "\x1b[31mRED\x1b[0mTEAM",
                 "score": "\x1b]0;s\x07100"},
                {"pos": "\x1b[33m2", "name": "ME", "score": 50},
            ],
        }
        out = render_panel(RankService._render_scoreboard(data))
        self.assertNotIn("\x1b", out)


# ======================================================================
# FINDING 2 — hai đường ghi global config hết load-stale-save
# ======================================================================
class TestF2ConfigCmdAtomicSet(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rev_c18_cfg_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.gc, self.cfg_file, restore = _patch_global_cfg(self._tmp)
        self.addCleanup(restore)

    def test_f2_set_does_not_trust_preloaded_stale_snapshot(self):
        # RED (MED): code cũ load_global_config() rồi save_global_config(cfg)
        # — nếu snapshot load bị stale (menu/CLI mở lâu, tiến trình khác vừa
        # ghi register_state/auth xuống đĩa) thì lần ĐẶT key đè MẤT dữ liệu
        # đó. Code mới đọc-mutate-ghi trong khóa: snapshot stale không được
        # tin dùng, state trên đĩa hiện hành phải sống sót.
        self.cfg_file.parent.mkdir()
        self.cfg_file.write_text(json.dumps({
            "register_state": {_URL: {"last_attempt_ts": 7}},
            "auth": {"/w1": {"cookie": "CK"}},
        }), encoding="utf-8")

        from ctf_downloader.cli import build_unified_parser
        from ctf_downloader.cli_commands import handle_config

        orig_load = self.gc.load_global_config
        self.gc.load_global_config = lambda: {}      # snapshot STALE rỗng
        self.addCleanup(setattr, self.gc, "load_global_config", orig_load)

        ns = build_unified_parser().parse_args(
            ["config", "auto-sync", "off"])
        handle_config(ns)

        saved = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual({"enabled": False}, saved["auto_sync"])
        self.assertEqual({_URL: {"last_attempt_ts": 7}},
                         saved.get("register_state"),
                         "register_state của tiến trình khác bị đè mất bởi "
                         "bản stale")
        self.assertEqual({"cookie": "CK"}, saved["auth"]["/w1"],
                         "auth entry có sẵn bị lost update")


class TestF2MenuSaveCurrentWorkspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rev_c18_menu_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.gc, self.cfg_file, restore = _patch_global_cfg(self._tmp)
        self.addCleanup(restore)

    def _make_menu(self, ws_dir):
        from ctf_downloader.interactive_menu import CTFInteractiveConsole
        menu = CTFInteractiveConsole.__new__(CTFInteractiveConsole)
        # self.config là SNAPSHOT chụp lúc mở menu — giờ đã stale so với đĩa.
        menu.config = {"workspaces": {}, "default_workspace": None, "auth": {}}
        menu.workspace_path = ws_dir
        menu.cookie = "CK_NEW"
        menu.token = None
        return menu

    def test_f2_menu_save_merges_into_fresh_disk_state(self):
        # RED (MED): menu mở cả phiên; giữa chừng `ctf register` (tiến trình
        # khác) ghi register_state + auth URL khác xuống config.json. Chọn
        # workspace rồi lưu phải merge TRÊN state đĩa HIỆN HÀNH qua
        # update_global_config — không được save lại snapshot stale đè mất.
        self.cfg_file.parent.mkdir()
        foreign = {
            "default_workspace": "/old",
            "register_state": {_URL: {"last_attempt_ts": 42}},
            "auth": {"/other": {"cookie": "FOREIGN"}},
        }
        self.cfg_file.write_text(json.dumps(foreign), encoding="utf-8")

        ws_dir = os.path.join(self._tmp, "wsA")
        os.makedirs(ws_dir)
        menu = self._make_menu(ws_dir)
        menu._save_current_workspace()

        saved = json.loads(self.cfg_file.read_text(encoding="utf-8"))
        self.assertEqual(ws_dir, saved["default_workspace"])
        self.assertEqual({"cookie": "CK_NEW", "token": None},
                         saved["auth"][ws_dir])
        self.assertEqual({_URL: {"last_attempt_ts": 42}},
                         saved["register_state"],
                         "register_state do tiến trình khác ghi GIỮA LÚC menu "
                         "mở bị đè mất")
        self.assertEqual({"cookie": "FOREIGN"}, saved["auth"]["/other"],
                         "auth entry của workspace khác bị lost update")
        # Cache nội bộ refresh từ state đĩa sau ghi — không còn stale.
        self.assertEqual(saved, menu.config)

    def test_f2_menu_save_oserror_logged_not_raised(self):
        # Menu KHÔNG crash khi storage hỏng — config.json là THƯ MỤC khiến
        # locked_update_json raise OSError khi đọc (deterministic, không phụ
        # thuộc user chạy test). Phải log lỗi rõ, không raise ra ngoài.
        self.cfg_file.parent.mkdir()
        self.cfg_file.mkdir()                        # file đích thành directory

        from ctf_downloader.utils.logger import Logger
        warns = []
        orig_warn = Logger.warning
        Logger.warning = staticmethod(lambda msg: warns.append(str(msg)))
        self.addCleanup(setattr, Logger, "warning", orig_warn)

        ws_dir = os.path.join(self._tmp, "wsB")
        os.makedirs(ws_dir)
        menu = self._make_menu(ws_dir)
        menu._save_current_workspace()               # không được raise
        self.assertTrue(warns, "lỗi ghi config phải được log warning")


# ======================================================================
# FINDING 3 — _commit_attempt phân biệt thua cuộc vs không persist được
# ======================================================================
class TestF3CommitAttemptStates(unittest.TestCase):
    def _svc(self, updater):
        return RegisterService(
            now_fn=lambda: 1_000_000.0,
            sleep_fn=lambda *_: None,
            config_loader=lambda: {},
            config_updater=updater,
            tempmail_factory=lambda: None,
            detect_fn=lambda url, session: (_OkPlatform(), _FakeInfo()))

    def test_f3_ok_and_race_states_unchanged(self):
        from ctf_downloader.storage.fileio import SKIP_WRITE

        def ok_updater(mutator):
            fresh = {}
            result = mutator(fresh)
            return copy.deepcopy(result)

        svc = self._svc(ok_updater)
        self.assertEqual(COMMIT_OK, svc._commit_attempt(_URL))

        def race_updater(mutator):
            # Đối thủ đã ghi attempt cùng URL ngay trước mình -> mutator trả
            # SKIP_WRITE, locked_update_json trả None.
            state = {"register_state":
                     {_URL: {"last_attempt_ts": 1_000_000.0}}}
            result = mutator(copy.deepcopy(state))
            return None if result is SKIP_WRITE else result

        svc = self._svc(race_updater)
        self.assertEqual(COMMIT_PREEMPTED, svc._commit_attempt(_URL))

    def test_f3_dir_missing_returns_unpersisted_not_success(self):
        # RED (LOW): updater mô phỏng locked_update_json khi thư mục chứa
        # config BIẾN MẤT — mutator chạy bình thường (không SKIP_WRITE) nhưng
        # kết quả vẫn None. Caller cũ chỉ nhìn preempted["wait"] -> báo
        # thành công dù KHÔNG persist được gì.
        def dir_gone_updater(mutator):
            mutator({})
            return None

        svc = self._svc(dir_gone_updater)
        self.assertEqual(COMMIT_UNPERSISTED, svc._commit_attempt(_URL))


# ======================================================================
# FINDING 4 — OSError không được nuốt credentials của account đã tạo
# ======================================================================
class TestF4OserrorKeepsCredentials(unittest.TestCase):
    def _svc(self, updater):
        return RegisterService(
            now_fn=lambda: 1_000_000.0,
            sleep_fn=lambda *_: None,
            config_loader=lambda: {},
            config_updater=updater,
            tempmail_factory=lambda: None,
            detect_fn=lambda url, session: (_OkPlatform(), _FakeInfo()))

    def _capture_warnings(self):
        from ctf_downloader.utils.logger import Logger
        warns = []
        orig_warn = Logger.warning

        def _rec(msg):
            warns.append(str(msg))

        Logger.warning = staticmethod(_rec)
        self.addCleanup(setattr, Logger, "warning", orig_warn)
        return warns

    def test_f4_reservation_storage_error_fails_closed_before_account_creation(self):
        # One-account safety now reserves BEFORE network. If global config
        # cannot persist that reservation, creating an account would re-open
        # the duplicate-account race, so the correct behavior is fail-closed.
        warns = self._capture_warnings()

        def broken_updater(mutator):
            raise PermissionError("config.json không đọc được")

        svc = self._svc(broken_updater)
        with self.assertRaises(RuntimeError) as ctx:
            svc.run(url=_URL, email="a@b.c")
        self.assertIn("TRƯỚC network POST", str(ctx.exception))
        self.assertTrue(any("register_state" in w or "global config" in w
                            for w in warns),
                        f"phải log warning rõ về việc không persist: {warns}")

    def test_f4_auth_save_oserror_logged_result_still_ok(self):
        # Commit attempt OK nhưng lần ghi auth map (thứ hai) hỏng — kết quả
        # register vẫn ok + warning rõ, không exception lan ra CLI.
        warns = self._capture_warnings()
        calls = {"n": 0}

        def flaky_updater(mutator):
            calls["n"] += 1
            if calls["n"] == 1:
                fresh = {}
                result = mutator(fresh)
                return copy.deepcopy(result)
            raise PermissionError("đĩa đầy")

        svc = self._svc(flaky_updater)
        result = svc.run(url=_URL, email="a@b.c")

        self.assertTrue(result["ok"])
        self.assertTrue(result["credentials"].get("username"))
        self.assertTrue(any("auth" in w for w in warns),
                        f"lỗi lưu auth phải được log warning: {warns}")


# ======================================================================
# FINDING 5 — backtick trong tên team không vỡ code-span markdown
# ======================================================================
class TestF5BacktickCodeSpan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rev_c18_md_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.svc = RankService.__new__(RankService)
        self.svc.workspace_path = self._tmp
        self.svc.repo = WorkspaceRepo(self._tmp)
        self.badges = []
        self.svc.repo.patch_summary_live_rank = \
            lambda badge: self.badges.append(badge) or False

    def _ranking_text(self):
        return (Path(self._tmp) / "RANKING.md").read_text("utf-8")

    def _team_line(self):
        return next(l for l in self._ranking_text().splitlines()
                    if "**Team**" in l)

    def test_f5_backtick_name_fence_widened(self):
        # RED (LOW): tên team ``we`rt`eam`` chứa backtick — code span đơn
        # `` `...` `` bị đóng sớm tại `` `rt` `` vỡ markdown.
        name = "we`rt`eam"
        data = {"title": "CTF", "my_team": name, "my_user": "-",
                "my_rank": 4, "my_score": 100, "total_teams": 10,
                "standings": [{"pos": 1, "name": name, "score": 100}]}
        self.svc._save_ranking_docs(data)

        self.assertEqual(f"- **Team**: ``{name}``", self._team_line(),
                         "backtick trong tên phải được bọc bằng delimiter "
                         "dài hơn (CommonMark)")
        self.assertIn(f"``{name}``", self.badges[0],
                      f"badge SUMMARY.md vỡ code-span: {self.badges[0]!r}")

    def test_f5_trailing_backtick_padded(self):
        data = {"title": "CTF", "my_team": "evil`", "my_user": "-",
                "my_rank": 4, "my_score": 100, "total_teams": 10,
                "standings": [{"pos": 1, "name": "evil`", "score": 100}]}
        self.svc._save_ranking_docs(data)
        # Mép phải là backtick -> CommonMark bắt buộc pad một space hai bên.
        self.assertEqual("- **Team**: `` evil` ``", self._team_line())

    def test_f5_clean_names_output_unchanged(self):
        # Regression với test_rank_repo byte-identical: dữ liệu sạch giữ
        # NGUYÊN dạng `value`.
        data = {"title": "CTF", "my_team": "ptit", "my_user": "huei",
                "my_rank": 2, "my_score": 1234, "total_teams": 10,
                "standings": [
                    {"pos": 1, "name": "Alpha", "score": 2000},
                    {"pos": 2, "name": "ptit", "score": 1234},
                ]}
        self.svc._save_ranking_docs(data)
        content = self._ranking_text()
        self.assertIn("- **Team**: `ptit`", content)
        self.assertIn("- **User**: `huei`", content)
        self.assertIn("- **Current Rank**: `#2` / `10 teams`", content)
        self.assertIn("- **Total Points**: `1234 pts`", content)
        self.assertIn("`#2`", self.badges[0])
        self.assertIn("(Team: `ptit`)", self.badges[0])


if __name__ == "__main__":
    unittest.main()
