"""HUNTER cycle-18 — các finding đã hunter xác nhận trong services:

1. [MED] rank_service._save_ranking_docs: my_rank/my_score/pos/score từ JSON
   server nhúng NGUYÊN vào RANKING.md (chỉ title/team qua md_cell) ->
   newline/pipe vỡ bảng markdown. Fix ở SINK: mọi giá trị gốc server đi qua
   md_cell.
2. [MED] register_service TOCTOU rate-limit: cfg load 1 lần rồi ghi stale
   sau network dài; hai CLI song song cùng URL đều pass check -> 2 tài khoản.
   Fix: re-check ngay trước ghi TRONG CÙNG khóa flock; save_global_config
   chuyển sang ghi atomic + lock tái sử dụng storage/fileio.
3. [MED] submit_service._update_local_workspace: README đọc/ghi bằng open()
   thô ngoài storage layer; except-pass nuốt dấu vết. Fix: qua fileio
   (atomic + lock), log warning thay vì nuốt.
4. [MED] submit_service.auto_scan_and_submit: nhiều candidate flag cùng 1
   challenge vẫn nộp tiếp sau khi flag đầu correct -> penalty risk. Fix:
   sau kết quả correct, re-load trạng thái solved rồi skip phần còn lại.
5. [LOW] rank_service: score không ép kiểu -> max() TypeError / so
   lexicographic khi server trả chuỗi. Fix: coerce an toàn, fallback 0.
6. [LOW] register_service._make_verify_hook: `or True` luôn verified. Fix:
   bỏ or True, kiểm HTTP status thật (200) — nhánh 404 phải False.
7. [LOW] register_service: captcha path PlatformRegisterUnsupported
   re-raise KHÔNG _record_attempt. Fix: ghi attempt trước re-raise.
8. [LOW] submit_service.resolve_challenge_id: identifier rỗng/toàn khoảng
   trắng khớp partial-match đầu tiên (`"" in key`). Fix: guard -> lỗi rõ.

TDD: các test đánh dấu RED tái hiện bug TRƯỚC khi sửa. Mọi network bị mock.
"""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.platforms.base import PlatformRegisterUnsupported
from ctf_downloader.services import submit_service as sub_mod
from ctf_downloader.services.register_service import RegisterService
from ctf_downloader.services.submit_service import SubmitService
from ctf_downloader.services.rank_service import RankService
from ctf_downloader.storage.fileio import SKIP_WRITE
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def render_panel(panel, width=100):
    """Render một rich renderable ra text thuần (như test_hunter_c14)."""
    buf = io.StringIO()
    from rich.console import Console
    from ctf_downloader.ui.theme import load_theme
    Console(file=buf, theme=load_theme(None), width=width, no_color=True,
            highlight=False).print(panel)
    return buf.getvalue()


def make_rank_svc(workspace=None):
    """RankService bỏ __init__ (không network/session/detector)."""
    svc = RankService.__new__(RankService)
    svc.workspace_path = workspace
    svc.repo = WorkspaceRepo(workspace) if workspace else None
    return svc


# ======================================================================
# FINDING 1 + 5 — rank_service: injection ở SINK RANKING.md + ép kiểu score
# ======================================================================
class TestRankDocsSinkEscaping(unittest.TestCase):
    DATA = {"title": "CTF", "my_team": "me", "my_user": "-", "my_rank": 4,
            "my_score": 100, "total_teams": 10, "standings": [
        {"pos": 1, "name": "me", "score": 100},
    ]}

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hunter_c18_rank_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.svc = make_rank_svc(self._tmp)
        self.svc.repo.patch_summary_live_rank = lambda *a, **k: False

    def _ranking_text(self):
        return (Path(self._tmp) / "RANKING.md").read_text("utf-8")

    def test_c18_01_pos_injection_keeps_table_shape(self):
        # RED (BUG-c18-1, M): pos do server trả chứa newline+pipe -> hàng
        # bảng vỡ: sinh thêm hàng giả "| FAKE | row |".
        data = dict(self.DATA)
        data["standings"] = [
            {"pos": "1\n| FAKE | row |", "name": "A", "score": 5},
        ]
        self.svc._save_ranking_docs(data)
        content = self._ranking_text()
        rows = [l for l in content.splitlines() if l.startswith("|")]
        # 1 hàng header + 1 separator + 1 dữ liệu; không được sinh hàng thừa
        self.assertEqual(3, len(rows),
                         f"pos injection sinh thêm hàng bảng: {rows}")
        for r in rows[2:]:
            self.assertEqual(5, len(r.split("|")),
                             f"hàng dữ liệu vỡ cột: {r!r}")
        self.assertNotIn("| FAKE | row |", content)
        self.assertIn("&#124;", rows[-1],
                      "pipe từ dữ liệu server phải thành thực thể HTML")

    def test_c18_02_header_rank_score_escaped(self):
        # RED (BUG-c18-1, M): my_rank/my_score nhúng NGUYÊN vào dòng bullet
        # đầu file — newline tách dòng, pipe sẵn sàng vỡ cấu trúc markdown.
        data = dict(self.DATA)
        data["my_rank"] = "4\n| INJECTED | x |"
        data["my_score"] = "50 | evil"
        self.svc._save_ranking_docs(data)
        content = self._ranking_text()
        self.assertNotIn("| INJECTED | x |", content,
                         "newline trong my_rank sinh dòng giả")
        rank_line = next(l for l in content.splitlines()
                         if "**Current Rank**" in l)
        points_line = next(l for l in content.splitlines()
                           if "**Total Points**" in l)
        for line in (rank_line, points_line):
            self.assertNotIn("|", line,
                             f"giá trị server phải qua md_cell (pipe -> "
                             f"thực thể): {line!r}")
        self.assertIn("&#124;", rank_line)
        self.assertIn("&#124;", points_line)

    def test_c18_03_string_scores_no_typeerror_correct_top(self):
        # RED (BUG-c18-5, L): score dạng chuỗi -> max() so lexicographic /
        # TypeError khi trừ ("300" - "300"), crash cả đường render.
        data = {"title": "CTF", "standings": [
            {"pos": 1, "name": "A", "score": "300"},
            {"pos": 2, "name": "B", "score": "1000"},
        ]}
        out = render_panel(make_rank_svc()._render_scoreboard(data))
        # top thật là 1000 (int) chứ không phải "300" (lexicographic);
        # gap của A = 700 pts.
        self.assertIn("-700 pts", out)

    def test_c18_04_my_score_string_footer_gap(self):
        # RED (BUG-c18-5, L): my_score chuỗi -> top_score - my_score raise
        # TypeError ngay dòng footer.
        data = {"title": "CTF", "my_rank": 9, "my_score": "50",
                "total_teams": 10, "standings": [
            {"pos": 1, "name": "A", "score": 1000},
        ]}
        out = render_panel(make_rank_svc()._render_scoreboard(data))
        self.assertIn("gap 950 pts", out)

    def test_c18_05_garbage_scores_fallback_zero_no_crash(self):
        # RED (BUG-c18-5, L): score rác (None/"abc") phải fallback 0 thay vì
        # làm max()/arithmetic nổ; "12.9" ép về 12.
        data = {"title": "CTF", "my_score": "abc", "standings": [
            {"pos": 1, "name": "N", "score": None},
            {"pos": 2, "name": "G", "score": "abc"},
            {"pos": 3, "name": "F", "score": "12.9"},
        ]}
        out = render_panel(make_rank_svc()._render_scoreboard(data))
        self.assertIn("12", out)          # "12.9" -> 12
        # docs path cũng không được nổ với cùng dữ liệu rác
        self.svc.repo.patch_summary_live_rank = lambda *a, **k: False
        self.svc._save_ranking_docs(data)  # chỉ cần không raise
        rows = [l for l in self._ranking_text().splitlines()
                if l.startswith("|")]
        for r in rows[2:]:
            self.assertEqual(5, len(r.split("|")), f"hàng vỡ: {r!r}")


# ======================================================================
# FINDING 2 + 6 + 7 — register_service: TOCTOU, hook status, captcha attempt
# ======================================================================
_URL = "https://gz.example.com"


class FakeInfo:
    platform_type = "gzctf"
    confidence = "high"


class FakePlatform:
    def __init__(self, side_effect=None):
        self.calls = 0
        self._side_effect = side_effect

    def register(self, *, username, email, password, verify_email_hook=None):
        self.calls += 1
        se = self._side_effect
        if isinstance(se, BaseException):
            raise se
        if callable(se):
            out = se(username=username, email=email, password=password)
            if isinstance(out, BaseException):
                raise out
            if out is not None:
                return out
        return {"ok": True, "message": "Registered"}


class RegisterCaseBase(unittest.TestCase):
    """Dựng RegisterService với store dict + updater mô phỏng đúng ngữ nghĩa
    locked_update_json (mutator nhận state FRESH, SKIP_WRITE = bỏ qua ghi)."""

    def setUp(self):
        self.now = [1_000_000.0]
        self.store = {}
        self.platform = FakePlatform()

        def fake_updater(mutator):
            fresh = copy.deepcopy(self.store)
            result = mutator(fresh)
            if result is SKIP_WRITE:
                return None
            self.store.clear()
            self.store.update(result)
            return copy.deepcopy(result)

        def detect(url, session):
            return self.platform, FakeInfo()

        self.svc = RegisterService(
            now_fn=lambda: self.now[0],
            sleep_fn=lambda *_: None,
            config_loader=lambda: copy.deepcopy(self.store),
            config_updater=fake_updater,
            tempmail_factory=lambda: None,
            detect_fn=detect)


class TestRateLimitTOCTOU(RegisterCaseBase):
    def test_c18_06_race_lost_reservation_stops_before_network(self):
        # Hai CLI có thể cùng pass snapshot _check_rate_limit(). Atomic
        # reservation phải re-read fresh state và phát hiện rival TRƯỚC khi
        # platform.register() tạo side effect phía server.
        rival_ts = self.now[0] + 25.0
        original_updater = self.svc._update_cfg
        injected = {"done": False}

        def rival_wins_before_reservation(mutator):
            if not injected["done"]:
                self.store.setdefault("register_state", {}).setdefault(
                    _URL, {})["last_attempt_ts"] = rival_ts
                self.store.setdefault("auth", {})["rival-key"] = {
                    "username": "rival"}
                injected["done"] = True
            return original_updater(mutator)

        self.svc._update_cfg = rival_wins_before_reservation
        with self.assertRaises(RuntimeError) as ctx:
            self.svc.run(url=_URL, email="a@b.c")
        self.assertIn("TRƯỚC network POST", str(ctx.exception))
        saved_ts = self.store.get("register_state", {}).get(_URL, {}) \
            .get("last_attempt_ts")
        self.assertEqual(rival_ts, saved_ts)
        self.assertIn("rival-key", self.store.get("auth", {}))
        self.assertEqual(0, self.platform.calls,
                         "thua reservation phải dừng trước register POST")

    def test_c18_07_sequential_after_60s_still_allowed(self):
        # Regression: đường commit mới vẫn cho phép chạy tuần tự chuẩn.
        r1 = self.svc.run(url=_URL, email="a@b.c")
        self.assertTrue(r1["ok"])
        self.now[0] += 61
        r2 = self.svc.run(url=_URL, email="a@b.c")
        self.assertTrue(r2["ok"])
        self.assertEqual(2, self.platform.calls)


class TestCaptchaAndHook(unittest.TestCase):
    def _service(self, store, platform, now_box):
        def fake_updater(mutator):
            fresh = copy.deepcopy(store)
            result = mutator(fresh)
            if result is SKIP_WRITE:
                return None
            store.clear()
            store.update(result)
            return copy.deepcopy(result)

        return RegisterService(
            now_fn=lambda: now_box[0],
            sleep_fn=lambda *_: None,
            config_loader=lambda: copy.deepcopy(store),
            config_updater=fake_updater,
            tempmail_factory=lambda: None,
            detect_fn=lambda url, session: (platform, FakeInfo()))

    def test_c18_08_captcha_unsupported_still_records_attempt(self):
        # RED (BUG-c18-7, L): nhánh captcha (PlatformRegisterUnsupported)
        # re-raise KHÔNG ghi attempt -> chạy lại liền nhau bypass rate limit.
        now = [1_000_000.0]
        store = {}
        plat = FakePlatform(side_effect=PlatformRegisterUnsupported(
            "captcha Turnstile — đăng ký thủ công"))
        svc = self._service(store, plat, now)
        with self.assertRaises(PlatformRegisterUnsupported):
            svc.run(url=_URL, email="a@b.c")
        ts = store.get("register_state", {}).get(_URL, {}) \
            .get("last_attempt_ts")
        self.assertEqual(now[0], ts,
                         "captcha path phải ghi attempt trước khi re-raise")

    def test_c18_09_verify_hook_checks_http_status(self):
        # RED (BUG-c18-6, L): `or True` khiến mọi status đều verified —
        # 404/500 phải trả False; 200 mới True.
        now = [1_000_000.0]
        svc = self._service({}, FakePlatform(), now)
        client = SimpleNamespace(
            wait_for_message=lambda timeout_s: {"id": "m1"},
            fetch_message_text=lambda mid:
                "Confirm: https://ctf.example.com/confirm/tok123",
        )
        hook = svc._make_verify_hook(client)

        class Sess:
            def __init__(self, code):
                self.code = code

            def get(self, url, timeout=None):
                return SimpleNamespace(status_code=self.code, url=url)

        self.assertTrue(hook(Sess(200)), "HTTP 200 = xác minh OK")
        self.assertFalse(hook(Sess(404)), "HTTP 404 KHÔNG được tính verified")
        self.assertFalse(hook(Sess(500)), "HTTP 500 KHÔNG được tính verified")

    def test_c18_10_generic_failure_still_records_attempt(self):
        # Regression (van-an-toàn R2) qua đường commit mới: exception thường
        # giữa flow vẫn phải ghi attempt (account có thể đã tồn tại).
        now = [1_000_000.0]
        store = {}
        plat = FakePlatform(side_effect=ValueError("boom giữa chừng"))
        svc = self._service(store, plat, now)
        with self.assertRaises(ValueError):
            svc.run(url=_URL, email="a@b.c")
        ts = store.get("register_state", {}).get(_URL, {}) \
            .get("last_attempt_ts")
        self.assertEqual(now[0], ts)


class TestGlobalConfigAtomic(unittest.TestCase):
    """Finding 2 (nửa storage): save_global_config atomic + lock;
    update_global_config RMW trong cùng khóa — tái sử dụng fileio."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hunter_c18_gcfg_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.gc = __import__(
            "ctf_downloader.storage.global_config", fromlist=["x"])
        self.cfg_file = os.path.join(self._tmp, "config.json")
        p1 = unittest.mock.patch.object(self.gc, "CONFIG_DIR", self._tmp)
        p2 = unittest.mock.patch.object(self.gc, "GLOBAL_CONFIG_FILE",
                                        self.cfg_file)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_c18_11_save_and_update_atomic_merge_defaults(self):
        self.gc.save_global_config({"workspaces": {"w": 1}})
        data = json.loads(Path(self.cfg_file).read_text("utf-8"))
        self.assertEqual({"w": 1}, data["workspaces"])
        # update_global_config merge trên state hiện hành + seed defaults
        result = self.gc.update_global_config(
            lambda d: d.setdefault("auth", {}).update({"k": "v"}) or d)
        self.assertEqual("v", result["auth"]["k"])
        data = json.loads(Path(self.cfg_file).read_text("utf-8"))
        self.assertEqual({"w": 1}, data["workspaces"])   # không mất dữ liệu cũ
        self.assertIsNone(data["default_workspace"])     # defaults được seed
        leftovers = [f for f in os.listdir(self._tmp)
                     if f.endswith((".tmp", ".lock"))]
        self.assertEqual([], leftovers, f"rác tmp/lock còn sót: {leftovers}")

    def test_c18_12_concurrent_updates_no_lost_update(self):
        errs = []

        def worker(i):
            try:
                def _mut(d):
                    d.setdefault("auth", {})[f"k{i}"] = i
                    return d
                self.gc.update_global_config(_mut)
            except Exception as exc:   # pragma: no cover
                errs.append(exc)

        barrier = threading.Barrier(8)
        threads = []
        for i in range(8):
            def run(i=i):
                barrier.wait(timeout=5)
                worker(i)
            th = threading.Thread(target=run)
            threads.append(th)
            th.start()
        for th in threads:
            th.join(timeout=30)
        self.assertEqual([], errs)
        data = json.loads(Path(self.cfg_file).read_text("utf-8"))
        self.assertEqual({f"k{i}": i for i in range(8)}, data["auth"],
                         "lost update: ghi song song ngoài khóa mất key")

    def test_c18_13_corrupt_config_backed_up_then_seeded(self):
        Path(self.cfg_file).write_text("{corrupt!!!", encoding="utf-8")
        result = self.gc.update_global_config(
            lambda d: d.update({"mark": 1}) or d)
        self.assertEqual(1, result["mark"])
        self.assertIn("auth", result)
        bak = Path(self.cfg_file + ".bak")
        self.assertTrue(bak.exists(), "file hỏng phải được backup .bak")
        self.assertEqual("{corrupt!!!", bak.read_text("utf-8"))

    def test_c18_14_real_file_commit_end_to_end(self):
        # Đường mặc định của RegisterService (không inject updater) phải ghi
        # vào global config THẬT qua update_global_config — dùng tmp paths.
        plat = FakePlatform()

        def detect(url, session):
            return plat, FakeInfo()

        svc = RegisterService(tempmail_factory=lambda: None, detect_fn=detect)
        res = svc.run(url=_URL, email="a@b.c")
        self.assertTrue(res["ok"])
        data = json.loads(Path(self.cfg_file).read_text("utf-8"))
        self.assertIn(_URL, data.get("register_state", {}))
        self.assertIn(_URL, data.get("auth", {}))
        self.assertEqual(res["credentials"]["username"],
                         data["auth"][_URL]["username"])


# ======================================================================
# FINDING 3 + 4 + 8 — submit_service
# ======================================================================
def make_submit_ws(prefix="hunter_c18_sub_"):
    ws = tempfile.mkdtemp(prefix=prefix)
    with open(os.path.join(ws, "challenges.json"), "w", encoding="utf-8") as f:
        json.dump({
            "platform_url": "http://ctf.test",
            "ctf_info": {"url": "http://ctf.test",
                         "flag_format": "^FLAG\\{.+\\}$",
                         "flag_format_source": "cache"},
            "challenges": [{"id": 1, "name": "One"},
                           {"id": 2, "name": "Two"}],
        }, f)
    return ws


def add_challenge(ws, cid, name, readme_text):
    d = os.path.join(ws, "Web", name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump({"id": cid, "name": name, "solved_by_me": False}, f)
    with open(os.path.join(d, "README.md"), "w", encoding="utf-8") as f:
        f.write(readme_text)
    return d


def make_submit_svc(ws, platform=None):
    platform = platform if platform is not None else unittest.mock.MagicMock()
    platform.ctf_info.platform_type = "ctfd"
    platform.authenticate.return_value = True
    platform.submit_flag.return_value = (True, "Correct!")
    platform.last_verdict = "correct"
    with unittest.mock.patch(
            "ctf_downloader.services.submit_service.create_session",
            return_value=unittest.mock.MagicMock()), \
         unittest.mock.patch(
            "ctf_downloader.services.submit_service.PlatformDetector"
            ".detect_platform", return_value=platform):
        svc = SubmitService(url="http://ctf.test", workspace_dir=ws)
    return svc, platform


class TestSubmitAutoScanGate(unittest.TestCase):
    def setUp(self):
        self.ws = make_submit_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_c18_15_stop_remaining_candidates_after_correct(self):
        # RED (BUG-c18-4, M): flag đầu correct -> candidate còn lại CÙNG
        # challenge vẫn bị nộp (penalty risk trên nhiều platform).
        add_challenge(self.ws, 1, "One",
                      "a: FLAG{aaa}\nb: FLAG{bbb}\n")
        svc, platform = make_submit_svc(self.ws)
        with unittest.mock.patch(
                "ctf_downloader.services.submit_service.time.sleep"):
            results = svc.auto_scan_and_submit()
        self.assertEqual(1, platform.submit_flag.call_count,
                         f"phải dừng sau flag đầu tiên đúng, results="
                         f"{[r['flag'] for r in results]}")
        self.assertEqual(1, len(results))
        self.assertEqual("submitted_ok", results[0]["category"])

    def test_c18_16_other_challenges_still_submitted(self):
        # Regression cho break: solved challenge A KHÔNG được chặn nộp
        # challenge B. (Flag B không dùng body placeholder như "xxx" —
        # auto-scan chủ động loại body đó.)
        add_challenge(self.ws, 1, "One", "a: FLAG{aaa}\nb: FLAG{aab}\n")
        add_challenge(self.ws, 2, "Two", "x: FLAG{bee}\n")
        svc, platform = make_submit_svc(self.ws)
        with unittest.mock.patch(
                "ctf_downloader.services.submit_service.time.sleep"):
            results = svc.auto_scan_and_submit()
        ok = [r for r in results if r["category"] == "submitted_ok"]
        self.assertEqual(2, platform.submit_flag.call_count,
                         "challenge B vẫn phải được nộp sau khi A solved")
        self.assertEqual(2, len(ok))
        self.assertEqual({1, 2}, {r["id"] for r in ok})


class TestResolveIdentifierGuard(unittest.TestCase):
    def setUp(self):
        self.ws = make_submit_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_c18_17_empty_identifier_no_partial_match(self):
        # RED (BUG-c18-8, L): "" / "   " khớp partial-match ĐẦU TIÊN
        # ("" in key luôn True) -> nộp flag bừa vào challenge ngẫu nhiên.
        svc, platform = make_submit_svc(self.ws)
        for bad in ("", "   ", "\t"):
            cid, name = svc.resolve_challenge_id(bad)
            self.assertIsNone(cid, f"identifier {bad!r} không được match bừa")
            ok, msg = svc.submit(bad, "FLAG{x}")
            self.assertFalse(ok)
            self.assertIn("Không tìm thấy challenge khớp", msg)
        self.assertEqual(0, platform.submit_flag.call_count)


class TestReadmeAtomicUpdate(unittest.TestCase):
    def setUp(self):
        self.ws = make_submit_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.chall_dir = add_challenge(
            self.ws, 1, "One", "- [ ] Solved\nFlag: FLAG{...}\nscore: FLAG{re}\n")

    def test_c18_18_placeholder_replaced_atomically(self):
        # BUG-c18-3 (M): README từng đọc/ghi bằng open() thô — giờ phải qua
        # fileio (atomic replace, không để lại .tmp) và thay placeholder.
        svc, platform = make_submit_svc(self.ws)
        with unittest.mock.patch(
                "ctf_downloader.services.submit_service.time.sleep"):
            ok, _msg = svc.submit(1, "FLAG{re}")
        self.assertTrue(ok)
        text = (Path(self.chall_dir) / "README.md").read_text("utf-8")
        self.assertIn("Flag: FLAG{re}", text)
        self.assertNotIn("FLAG{...}", text)
        self.assertIn("- [x] Solved", text)
        leftovers = [f for f in os.listdir(self.chall_dir)
                     if f.endswith((".tmp", ".lock"))]
        self.assertEqual([], leftovers, f"rác ghi còn sót: {leftovers}")

    def test_c18_19_write_failure_logged_not_swallowed(self):
        # RED (BUG-c18-3, M): except-pass :699 nuốt sạch dấu vết — lỗi ghi
        # README phải log warning, và submit không vì thế mà crash.
        svc, platform = make_submit_svc(self.ws)

        def boom(path, text):
            raise OSError("disk full")

        warnings = []
        with unittest.mock.patch(
                "ctf_downloader.services.submit_service.time.sleep"), \
             unittest.mock.patch(
                "ctf_downloader.services.submit_service.atomic_write_text",
                side_effect=boom), \
             unittest.mock.patch.object(
                sub_mod.Logger, "warning",
                side_effect=lambda *a, **k: warnings.append(a)):
            ok, _msg = svc.submit(1, "FLAG{re}")
        self.assertTrue(ok, "lỗi tài liệu local không được ảnh hưởng kết quả")
        self.assertTrue(warnings,
                        "lỗi ghi README bị nuốt lặng lẽ (except-pass)")
        joined = " ".join(str(a) for a in warnings)
        self.assertIn("FLAG", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)
