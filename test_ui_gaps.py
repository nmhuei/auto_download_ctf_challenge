"""test_ui_gaps.py — lấp khoảng trống test sau UI overhaul (nhánh
rebuild/architecture). KHÔNG đụng các test file cũ.

Cover 6 path chưa có coverage ổn định:
  1. Watch degrade: terminal < DEGRADE_WIDTH (80) cột bỏ mini-scoreboard,
     vẫn giữ notices + countdown + footer (mock guard/state, không mạng).
  2. Register gặp captcha Turnstile (shape thật GZCTF:
     GET /api/captcha -> {"type": "Turnstile", "siteKey": ...}) → dừng sạch,
     raise PlatformRegisterUnsupported và in hướng dẫn thủ công.
  3. Sniper --start-at ISO trong tương lai → không bao giờ bắn trước giờ G
     (FakeClock thay toàn bộ time của sniper_service).
  4. Export-pack INDEX.md escape tên challenge chứa ``[bold]`` — chống
     markdown injection (không vỡ bảng/không sinh markup).
  5. History: mặc định redact flag (4 ký tự đầu + ***), --all hiện đầy đủ.
  6. Storage archive với workspace chứa symlink dir → KHÔNG follow symlink
     (nội dung ngoài workspace không lọt vào tar.gz).

Mọi HTTP đều mock (FakeSession) — không gọi mạng thật.

Chạy: python3 -m pytest test_ui_gaps.py -q
"""
import contextlib
import datetime as _dt
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import time as _real_time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ctf_downloader.platforms.base import PlatformRegisterUnsupported
from ctf_downloader.platforms.gzctf import GZCTFPlatform
from ctf_downloader.services import register_service as reg_mod
from ctf_downloader.services import sniper_service as sn_mod
from ctf_downloader.services.register_service import RegisterService
from ctf_downloader.services.sniper_service import SniperService
from ctf_downloader.services.storage_manager import StorageManager
from ctf_downloader.services.watch_service import (
    DEGRADE_WIDTH,
    WatchService,
    WindowGuard,
)
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


# ---------------------------------------------------------------------- #
# Helpers dùng chung
# ---------------------------------------------------------------------- #
def make_console(width=200):
    """Console rich ghi ra buffer (theme PHOSPHOR) để assert output."""
    from rich.console import Console

    from ctf_downloader.ui.theme import load_theme

    buf = io.StringIO()
    con = Console(file=buf, width=width, theme=load_theme(None),
                  force_terminal=False, highlight=False)
    return con, buf


def render_plain(renderable, width=100):
    """Render rich renderable ra text thuần (không ANSI) để assert."""
    from rich.console import Console

    buf = io.StringIO()
    console = Console(width=width, file=buf, force_terminal=False,
                      legacy_windows=False)
    console.print(renderable)
    return buf.getvalue()


def write_challenges_json(ws, title="GAP CTF 2026", event_window=None):
    ctf_info = {"title": title, "url": "https://ctf.gap.test"}
    if event_window is not None:
        ctf_info["event_window"] = event_window
    with open(os.path.join(ws, "challenges.json"), "w", encoding="utf-8") as f:
        json.dump({"platform_url": "https://ctf.gap.test",
                   "ctf_info": ctf_info, "challenges": []}, f)


class TempWorkspaceCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ui_gaps_")
        self.ws = os.path.join(self._tmp, "ws")
        os.makedirs(self.ws)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


# ---------------------------------------------------------------------- #
# 1. Watch degrade — width < 80 bỏ mini-scoreboard
# ---------------------------------------------------------------------- #
class TestWatchDegradeNarrow(TempWorkspaceCase):
    """Mock guard/state rồi render panel ở 2 độ rộng — bản narrow (<80 cột)
    phải bỏ scoreboard nhưng vẫn còn header/notices/footer."""

    def _svc(self):
        write_challenges_json(self.ws)   # tên giải từ challenges.json
        svc = WatchService(str(self.ws), once=True, use_live_ui=False)
        svc.platform = SimpleNamespace(
            ctf_info=SimpleNamespace(platform_type="gzctf"),
            base_url="https://ctf.gap.test",
            session=MagicMock())
        now = _dt.datetime.now(_dt.timezone.utc)
        svc.guard = WindowGuard(now - _dt.timedelta(hours=1),
                                now + _dt.timedelta(hours=4))
        # State giả lập đã tick scoreboard/notices
        svc._mini_sb_rows = [{"pos": i + 1, "name": f"Team{i}", "score": 900 - 100 * i}
                             for i in range(5)]
        svc._known_chall_count = 3
        svc._feed.append("📢 Gap gap gap")
        self.assertLess(70, DEGRADE_WIDTH)
        self.assertGreaterEqual(100, DEGRADE_WIDTH)
        return svc

    def test_narrow_drops_scoreboard_wide_keeps_it(self):
        svc = self._svc()
        wide = render_plain(svc._render_panel([], width=100), width=100)
        narrow = render_plain(svc._render_panel([], width=70), width=70)

        # Wide: mini-scoreboard top-5 + meter gradient có mặt
        self.assertIn("🏆", wide)
        self.assertIn("Team0", wide)
        self.assertIn("▰", wide)
        # Narrow (<80): bỏ hẳn mini-scoreboard
        self.assertNotIn("🏆", narrow)
        self.assertNotIn("▰", narrow)

    def test_narrow_keeps_notices_countdown_and_footer(self):
        svc = self._svc()
        narrow = render_plain(svc._render_panel([], width=70), width=70)
        self.assertIn("📢 Gap gap gap", narrow)          # notices feed giữ
        self.assertIn("🔴 LIVE", narrow)                 # trạng thái window
        self.assertIn("kết thúc sau", narrow)            # countdown
        self.assertIn("SỰ KIỆN", narrow)                 # khu notices
        for binding in ("q thoát", "p pause", "r refresh-now"):
            self.assertIn(binding, narrow)               # footer bar giữ

    def test_degrade_boundary_exactly_80_still_shows_scoreboard(self):
        svc = self._svc()
        at_limit = render_plain(svc._render_panel([], width=DEGRADE_WIDTH),
                                width=DEGRADE_WIDTH)
        self.assertIn("🏆", at_limit)


# ---------------------------------------------------------------------- #
# 2. Register — Turnstile captcha dừng sạch + hướng dẫn thủ công
# ---------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text or (json.dumps(json_data)
                             if json_data is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Session giả: map (method, url-contains) -> FakeResponse."""

    def __init__(self, routes=None):
        self.routes = routes or []
        self.calls = []
        self.cookies = {}

    def _handle(self, method, url, **kw):
        self.calls.append((method, url))
        best, best_len = None, -1
        for m, sub, resp in self.routes:
            if m == method and sub in url and len(sub) > best_len:
                best, best_len = resp, len(sub)
        return best or FakeResponse(404, text="not found")

    def get(self, url, timeout=None, **kw):
        return self._handle("GET", url, **kw)

    def post(self, url, timeout=None, **kw):
        return self._handle("POST", url, **kw)


class TestRegisterTurnstileManualGuide(TempWorkspaceCase):
    def test_turnstile_captcha_stops_with_manual_instruction(self):
        # Shape thật GZCTF hiện đại: /api/config không khai báo provider,
        # /api/captcha trả {"type": "Turnstile", "siteKey": ...}.
        sess = FakeSession([
            ("GET", "/api/config", FakeResponse(
                200, json_data={"Title": "GAP CTF"})),
            ("GET", "/api/captcha", FakeResponse(
                200, json_data={"type": "Turnstile",
                                "siteKey": "0xGAPTURNSTILEKEY"})),
        ])
        platform = GZCTFPlatform("https://gz.example.com/games/6/challenges",
                                 sess)
        info = SimpleNamespace(platform_type="gzctf", confidence=0.95)
        saved = []

        con, buf = make_console()
        svc = RegisterService(config_loader=lambda: {},
                              config_saver=saved.append,
                              tempmail_factory=lambda: (_ for _ in ()).throw(
                                  AssertionError("tempmail không được dùng")),
                              detect_fn=lambda url, session: (platform, info))

        with patch.object(reg_mod, "console", con), \
             patch("ctf_downloader.services.session_factory.create_session",
                   return_value=MagicMock()):
            with self.assertRaises(PlatformRegisterUnsupported) as ctx:
                svc.run("https://gz.example.com/games/6/challenges",
                        email="gap@ctf.test")

        # Exception message nêu rõ thủ công + URL đăng ký + không bypass
        msg = str(ctx.exception)
        self.assertIn("đăng ký thủ công", msg)
        self.assertIn("https://gz.example.com/register", msg)
        self.assertIn("không bypass captcha", msg)

        out = buf.getvalue()
        # Output chứa hướng dẫn thủ công bằng trình duyệt + credentials
        self.assertIn("đăng ký thủ công bằng trình duyệt", out)
        self.assertIn("USERNAME", out)
        self.assertIn("PASSWORD", out)
        self.assertIn("gap@ctf.test", out)
        # KHÔNG có ✔ tạo tài khoản (created=False)
        self.assertNotIn("Đã tạo tài khoản", out)
        # Không POST register/login nào được thực hiện
        self.assertFalse(any(m == "POST" for m, _u in sess.calls),
                         f"đã gọi POST khi có captcha: {sess.calls}")
        # Attempt KHÔNG được ghi rate-limit (raise trước khi record)
        self.assertEqual(saved, [])


# ---------------------------------------------------------------------- #
# 3. Sniper --start-at ISO tương lai — không bắn trước giờ G
# ---------------------------------------------------------------------- #
class FakeClock:
    """Đồng hồ giả cho sniper_service: time() trả giờ giả, sleep() tăng."""

    def __init__(self, start=1_700_000_000.0):
        self.now = float(start)
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(max(0.0, float(seconds)))
        self.now += max(0.0, float(seconds))

    strftime = staticmethod(_real_time.strftime)
    gmtime = staticmethod(_real_time.gmtime)


class FakeSniperSubmitter:
    def __init__(self, script, clock):
        self.script = list(script)
        self.clock = clock
        self.calls = []
        self.call_times = []
        self.platform = SimpleNamespace(last_verdict=None)
        self.submit_history = []

    def submit(self, challenge, flag, force=False):
        verdict, message = self.script.pop(0)
        self.platform.last_verdict = verdict
        self.calls.append((challenge, flag, force))
        self.call_times.append(self.clock.time())
        if verdict in ("correct", "incorrect"):
            self.submit_history.append({"flag": flag, "result": verdict})
        return verdict in ("correct", "ratelimited"), message


class TestSniperFutureStartAt(TempWorkspaceCase):
    def test_future_start_at_never_fires_before_g(self):
        clock = FakeClock(start=1_700_000_000.0)
        start_epoch = clock.now + 500   # giờ G cách 500s theo đồng hồ giả
        start_iso = _dt.datetime.fromtimestamp(
            start_epoch, tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # challenges.json KHÔNG có event_window → bắt buộc dùng --start-at
        write_challenges_json(self.ws, event_window=None)
        with open(os.path.join(self.ws, "sniper.json"), "w",
                  encoding="utf-8") as f:
            json.dump([{"challenge": 1, "flag": "GAP{first_blood}",
                        "delay_seconds": 0}], f)

        submitter = FakeSniperSubmitter([("correct", "chính xác")], clock)
        service = SniperService(WorkspaceRepo(self.ws), submitter)

        with patch.object(sn_mod, "time", clock), \
             patch.object(sn_mod, "console", make_console()[0]):
            summary = service.run(poll_interval=30, start_at=start_iso)

        self.assertEqual(len(summary["solved"]), 1)
        self.assertEqual(submitter.calls, [(1, "GAP{first_blood}", False)])
        # Bất biến van-an-toàn #1: mọi phát bắn >= giờ G
        self.assertGreaterEqual(min(submitter.call_times), start_epoch - 1e-6)
        # started_at đúng ISO truyền vào
        self.assertEqual(summary["started_at"],
                         _real_time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                             _real_time.gmtime(start_epoch)))
        # Đã chờ qua các nhịp poll ngắn (không ngủ một lần 500s > interval)
        self.assertTrue(clock.sleeps)
        self.assertLessEqual(max(s for s in clock.sleeps), 30 + 1e-6)


# ---------------------------------------------------------------------- #
# 4. Export-pack INDEX.md — markdown injection escape
# ---------------------------------------------------------------------- #
class TestExportPackIndexEscape(TempWorkspaceCase):
    CHALL_NAME = "Evil [bold](http://evil.example) Pwn"

    def _make_ws(self):
        ws = self.ws
        write_challenges_json(ws, title="GAP CTF")
        chal_dir = Path(ws) / "Misc" / "Evil_Chall"
        (chal_dir / "writeup").mkdir(parents=True)
        (chal_dir / "metadata.json").write_text(json.dumps({
            "id": 7, "name": self.CHALL_NAME, "category": "Mi[sc]",
            "points": 100,
            "status": {"schema_version": 2, "solve": "solved_by_me",
                       "flag": {"value": None, "state": "none"},
                       "writeup": "complete", "writeup_auto": True},
        }, ensure_ascii=False), encoding="utf-8")
        (chal_dir / "writeup" / "README.md").write_text(
            "# W\n\nFlag: `PTIT{esc4pe_inject}`\n", encoding="utf-8")

    def test_index_md_escapes_markdown_specials_in_name_and_category(self):
        from ctf_downloader.services.writeup_exporter import WriteupExporter

        self._make_ws()
        out_dir = Path(self._tmp) / "out"
        pack_dir = WriteupExporter(self.ws).build_pack(out_dir=out_dir)
        index = (pack_dir / "INDEX.md").read_text(encoding="utf-8")

        # Tên/category được backslash-escape — không còn raw "[bold](...)"
        escaped_name = self.CHALL_NAME.replace("[", "\\[").replace("]", "\\]")
        self.assertIn(escaped_name.split("(")[0].strip(), index.replace("(", " ("))
        self.assertNotIn("[bold]", index)
        self.assertNotIn("[Mi[sc]]", index)
        self.assertIn("\\[bold\\]", index)
        self.assertIn("\\[sc\\]", index)
        # Link README per-entry vẫn trỏ tới dirname đã sanitize
        # ("Mi[sc]" + tên evil → sanitize_folder_name rồi [()] → '_').
        entry_dir = "Mi_sc__Evil__bold__http_evil.example__Pwn"
        self.assertIn(f"[{entry_dir}/README.md]({entry_dir}/README.md)", index)
        # Bảng không bị vỡ: dòng challenge (bắt đầu "| ") vẫn nằm giữa
        # 2 dấu | đầu/cuối — loại trừ mục list "Chi tiết từng bài".
        row_lines = [ln for ln in index.splitlines()
                     if ln.startswith("| ") and "\\[bold\\]" in ln]
        self.assertTrue(row_lines)
        for ln in row_lines:
            self.assertTrue(ln.startswith("| ") and ln.rstrip().endswith(" |"),
                            f"dòng bảng vỡ: {ln!r}")


# ---------------------------------------------------------------------- #
# 5. History — redact mặc định vs --all
# ---------------------------------------------------------------------- #
class TestHistoryFlagRedaction(TempWorkspaceCase):
    HIST = {"entries": [
        {"flag": "PTITCTF{sup3r_secret_flag}", "challenge_id": 12,
         "result": "correct", "timestamp": "2026-08-24T09:00:00Z"},
    ]}

    def _run_history(self, show_all):
        from ctf_downloader import cli_commands

        ns = SimpleNamespace(workspace=self.ws, show_all=show_all)
        with patch.object(WorkspaceRepo, "load_submit_history",
                          return_value=dict(self.HIST)), \
             patch.object(WorkspaceRepo, "find_challenge",
                          return_value={"name": "Chall A"}):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                cli_commands.handle_history(ns)
        return buf.getvalue()

    def test_default_redacts_flag(self):
        out = self._run_history(show_all=False)
        self.assertIn("PTIT***", out)
        self.assertNotIn("sup3r_secret_flag", out)
        self.assertIn("--all", out)   # gợi ý bật hiển thị đầy đủ

    def test_show_all_reveals_full_flag(self):
        out = self._run_history(show_all=True)
        self.assertIn("PTITCTF{sup3r_secret_flag}", out)
        self.assertNotIn("PTIT***", out)


# ---------------------------------------------------------------------- #
# 6. Storage archive — workspace chứa symlink dir KHÔNG bị follow
# ---------------------------------------------------------------------- #
class TestArchiveSkipsSymlinkDir(TempWorkspaceCase):
    def test_symlinked_dir_contents_not_archived(self):
        ws = os.path.join(self._tmp, "CTF_WS")
        os.makedirs(os.path.join(ws, "Web", "c1"))

        real_file = os.path.join(ws, "Web", "c1", "challenge.zip")
        with open(real_file, "wb") as f:
            f.write(b"A" * 1234)

        # Thư mục ngoài workspace chứa file "bí mật"
        outside = os.path.join(self._tmp, "outside")
        os.makedirs(os.path.join(outside, "deep"))
        secret = os.path.join(outside, "deep", "secret.txt")
        with open(secret, "wb") as f:
            f.write(b"TOPSECRET" * 100)

        # Symlink dir bên trong workspace trỏ ra ngoài
        link = os.path.join(ws, "leaked")
        os.symlink(outside, link, target_is_directory=True)
        self.assertTrue(os.path.islink(link))

        out_dir = os.path.join(self._tmp, "out")
        result = StorageManager.archive_workspace(ws, out_dir)
        arc = result["archive_path"]
        self.assertTrue(os.path.isfile(arc))

        with tarfile.open(arc, "r:gz") as tf:
            names = tf.getnames()
            members = {m.name: m for m in tf.getmembers()}

        joined = "\n".join(names)
        self.assertIn("Web/c1/challenge.zip", names)
        # Không follow symlink: nội dung ngoài workspace không lọt vào tar
        self.assertNotIn("secret.txt", joined)
        self.assertFalse(any("deep" in n for n in names),
                         f"symlink bị follow: {names}")
        # Dung lượng tính chỉ từ file thật (symlink dir bị bỏ qua hoàn toàn)
        self.assertEqual(result["original_bytes"], 1234)
        # Giải nén thử: không xuất hiện file bí mật
        extract_dir = os.path.join(self._tmp, "extract")
        os.makedirs(extract_dir)
        safe_members = [m for m in members.values()
                        if not os.path.isabs(m.name) and ".." not in m.name]
        with tarfile.open(arc, "r:gz") as tf:
            try:
                tf.extractall(extract_dir, members=safe_members, filter="data")
            except TypeError:   # Python < 3.12 không có filter=
                tf.extractall(extract_dir, members=safe_members)
        extracted = "\n".join(
            str(p.relative_to(extract_dir))
            for p in Path(extract_dir).rglob("*"))
        self.assertIn("challenge.zip", extracted)
        self.assertNotIn("secret", extracted)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------- #
# N. Pull pipe — stdin đóng + thiếu --url phải exit sạch, không EOFError
# ---------------------------------------------------------------------- #
class TestPullPipeNoTty(unittest.TestCase):
    """Live-verify v4: `ctf pull </dev/null` rẽ launch_interactive_menu()
    rồi nổ EOFError traceback thô. Non-tty phải exit 2 kèm hint."""

    def _run_handle_pull(self):
        import io as _io
        from argparse import Namespace
        from ctf_downloader import cli_commands

        ns = Namespace(interactive=False, url=None, cookie=None, token=None,
                       output=None, threads=4, no_third_party=False,
                       no_template=False, force=False, timeout=30,
                       category=None, exclude=None, update=False,
                       refresh_meta=False)
        fake_stdin = type("FakeStdin", (), {"isatty": lambda self: False})()
        orig_stdin = sys.stdin
        sys.stdin = fake_stdin
        try:
            with contextlib.redirect_stdout(_io.StringIO()), \
                    contextlib.redirect_stderr(_io.StringIO()), \
                    patch.object(cli_commands, "launch_interactive_menu") as m_menu, \
                    patch.object(cli_commands.Logger, "error") as m_err, \
                    patch.object(cli_commands.Logger, "info") as m_info:
                with self.assertRaises(SystemExit) as cm:
                    cli_commands.handle_pull(ns)
                m_menu.assert_not_called()
        finally:
            sys.stdin = orig_stdin
        return cm.exception.code, " ".join(
            str(c.args) for c in (*m_err.call_args_list,
                                  *m_info.call_args_list))

    def test_pipe_no_url_exits_2_with_hint(self):
        code, msgs = self._run_handle_pull()
        self.assertEqual(code, 2)
        self.assertIn("--url", msgs)

    def test_pipe_interactive_flag_also_refuses(self):
        import io as _io
        from argparse import Namespace

        from ctf_downloader import cli_commands

        ns = Namespace(interactive=True, url="https://x.example", cookie=None,
                       token=None, output=None, threads=4, no_third_party=False,
                       no_template=False, force=False, timeout=30,
                       category=None, exclude=None, update=False,
                       refresh_meta=False)
        fake = type("F", (), {"isatty": lambda self: False})()
        orig = sys.stdin
        sys.stdin = fake
        try:
            with contextlib.redirect_stderr(io.StringIO()), \
                    patch.object(cli_commands, "launch_interactive_menu") as m_menu:
                with self.assertRaises(SystemExit) as cm:
                    cli_commands.handle_pull(ns)
                m_menu.assert_not_called()
        finally:
            sys.stdin = orig
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
