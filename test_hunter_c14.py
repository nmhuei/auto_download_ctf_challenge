"""HUNTER cycle-14 — các surface CHƯA từng bị quét trực tiếp:

1. Rank scoreboard render (rank_service._render_scoreboard) — tên dài /
   ANSI escape sẵn / điểm None-âm-1e18 / tie-break / 0 teams / top_n vượt.
2. RANKING.md qua WorkspaceRepo.write_ranking_md (commit 6267d42) —
   markdown table vỡ khi team name chứa ``|`` / newline; đua
   patch_summary_live_rank với writer khác của SUMMARY.md.
3. History (handle_history) — biên limit/redact/result lạ/entry thiếu field.
4. SummaryGenerator — category/name chứa ``|`` hoặc newline, points float,
   solved_by_me=True + writeup='none'.
5. Cross-check print_table passthrough non-str.

Mọi network bị mock/bypass (RankService dựng qua __new__ như test_rank_repo).
Quy ước: FAIL có chủ ý = bug thật đang tái hiện (đánh dấu EXPECTED FAIL);
PASS mang tính documentation khi hành vi hiện tại là thiết kế đúng.
"""
import contextlib
import inspect
import io
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import unittest
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.generator.summary_generator import SummaryGenerator
from ctf_downloader.services import rank_service as rs
from ctf_downloader.services.rank_service import RankService
from ctf_downloader.storage import fileio
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def render_panel(panel, width=100):
    """Render một rich renderable ra text thuần (no_color để ESC còn sót lại
    trong output chắc chắn là ESC do DỮ LIỆU mang vào, không phải style).
    Console phải load theme chung để token accent.deep/fg.base resolve."""
    buf = io.StringIO()
    from rich.console import Console
    from ctf_downloader.ui.theme import load_theme
    Console(file=buf, theme=load_theme(None), width=width, no_color=True,
            highlight=False).print(panel)
    return buf.getvalue()


def make_svc(workspace=None):
    """RankService bỏ __init__ (không network/session/detector)."""
    svc = RankService.__new__(RankService)
    svc.workspace_path = workspace
    svc.repo = WorkspaceRepo(workspace) if workspace else None
    return svc


# ======================================================================
# 1. SCOREBOARD RENDER
# ======================================================================
class TestScoreboardRender(unittest.TestCase):
    def _render(self, data, top_n=15):
        return render_panel(make_svc()._render_scoreboard(data, top_n=top_n))

    def test_c14_01_long_team_name_no_crash(self):
        """Tên team > width: phải render được, không crash (wrap/truncate là
        việc của rich)."""
        data = {"title": "CTF", "standings": [
            {"pos": 1, "name": "T" * 300, "score": 100},
            {"pos": 2, "name": "ok", "score": 50},
        ]}
        out = self._render(data)
        self.assertIn("TTTT", out.replace("\n", ""))

    def test_c14_02_ansi_escape_not_passed_through(self):
        # EXPECTED FAIL (BUG-C14-1, M): tên team do SERVER kiểm soát (đối thủ)
        # chứa ANSI escape -> \x1b[31m đi NGUYÊN vào output terminal
        # (terminal injection: đổi màu/clear screen/OSC title).
        data = {"title": "T\x1b[31mITLE", "standings": [
            {"pos": 1, "name": "\x1b[31mRED\x1b[0mTEAM", "score": 100},
        ]}
        out = self._render(data)
        self.assertNotIn("\x1b[", out,
                         "ESC từ dữ liệu server phải được strip trước khi render")

    def test_c14_03_score_none_negative_huge(self):
        """score None -> hiển thị 0; âm/1e18 không crash."""
        data = {"title": "CTF", "my_rank": 4, "my_score": -10,
                "total_teams": 4, "standings": [
            {"pos": 1, "name": "A", "score": None},
            {"pos": 2, "name": "B", "score": -50},
            {"pos": 3, "name": "C", "score": 10 ** 18},
            {"pos": 4, "name": "D", "score": float(10 ** 18)},
        ]}
        out = self._render(data)  # chỉ cần không raise
        self.assertIn("-50", out)

    def test_c14_04_unsorted_standings_gap_format(self):
        # EXPECTED FAIL (BUG-C14-7, L): standings chưa sort từ server ->
        # gap_pts âm in ra "--50 pts"; footer lại max(0,...) nên hai đường
        # format KHÔNG nhất quán.
        data = {"title": "CTF", "standings": [
            {"pos": 1, "name": "A", "score": 50},
            {"pos": 2, "name": "B", "score": 100},
        ]}
        out = self._render(data)
        self.assertNotIn("--", out, "gap không được in thành '--N pts'")

    def test_c14_05_full_tie_all_gaps_dash(self):
        data = {"title": "CTF", "my_team": "ME", "my_user": None,
                "my_rank": 1, "my_score": 100, "total_teams": 3,
                "standings": [
            {"pos": 1, "name": "A", "score": 100},
            {"pos": 2, "name": "B", "score": 100},
            {"pos": 3, "name": "ME", "score": 100},
        ]}
        out = self._render(data)
        self.assertNotIn("--", out)          # không gap lố
        self.assertIn("gap 0 pts", out)      # footer tie = 0
        self.assertEqual(out.count("-"), out.count("-"))  # smoke

    def test_c14_06_zero_teams_empty_state(self):
        data = {"title": "CTF", "standings": []}
        out = self._render(data)
        self.assertIn("chưa có dữ liệu", out)

    def test_c14_07_top_n_larger_than_standings(self):
        data = {"title": "CTF", "standings": [
            {"pos": i, "name": f"T{i}", "score": i} for i in range(1, 6)
        ]}
        out = self._render(data, top_n=9999)
        for i in range(1, 6):
            self.assertIn(f"T{i}", out)

    def test_c14_08_dead_code_after_return_documented(self):
        # DOCUMENTATION (DOC-C14-6, L): _render_scoreboard còn khối
        # ``if update_docs ...`` + ``return data`` SAU ``return Panel(...)``
        # (rank_service.py ~221-224) — unreachable, và nếu reachable sẽ
        # NameError vì staticmethod không có param update_docs.
        src = inspect.getsource(RankService._render_scoreboard)
        idx_panel = src.index("return Panel(")
        tail = src[idx_panel:]
        self.assertNotIn("update_docs", tail,
                         "code chết sau return Panel cần bị xoá")


# ======================================================================
# 2. RANKING.md QUA REPO + ĐUA SUMMARY
# ======================================================================
class TestRankingMdInjection(unittest.TestCase):
    DATA = {"title": "CTF", "my_team": "me", "my_user": "-", "my_rank": 1,
            "my_score": 100, "total_teams": 2, "standings": [
        {"pos": 1, "name": "me", "score": 100},
        {"pos": 2, "name": "EVIL|NAME", "score": 50},
    ]}

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hunter_c14_rank_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.svc = make_svc(self._tmp)
        self.svc.repo.patch_summary_live_rank = lambda *a, **k: False

    def _ranking_rows(self):
        content = (Path(self._tmp) / "RANKING.md").read_text("utf-8")
        return [l for l in content.splitlines() if l.startswith("|")]

    def test_c14_09_pipe_in_name_keeps_table_shape(self):
        # EXPECTED FAIL (BUG-C14-2, M): team name chứa '|' -> hàng bảng
        # sinh thêm cột ảo, RANKING.md vỡ bảng khi render markdown.
        self.svc._save_ranking_docs(dict(self.DATA))
        rows = self._ranking_rows()
        bad = [r for r in rows[2:] if len(r.split("|")) != 5]  # 3 cột -> 5 mảnh
        self.assertEqual([], bad, f"hàng vỡ bảng: {bad}")

    def test_c14_10_newline_in_name_stays_one_row(self):
        # BUG-C14-2 cùng họ: newline trong tên đội KHÔNG được sinh hàng bảng
        # giả. Lưu ý writer GHI ĐÈ toàn bộ RANKING.md mỗi lần lưu nên mốc
        # so sánh đúng là cấu trúc tĩnh của lần lưu CUỐI (2 hàng header +
        # 1 hàng dữ liệu), không phải "before + 1" như bản red đầu tiên.
        data = dict(self.DATA)
        data["standings"] = [{"pos": 2, "name": "EVIL\nNAME", "score": 50}]
        self.svc._save_ranking_docs(data)
        after = self._ranking_rows()
        self.assertEqual(3, len(after),
                         f"newline trong tên sinh thêm hàng bảng: {after}")
        self.assertTrue(any("EVIL NAME" in r for r in after),
                         f"tên phải nằm gọn MỘT hàng (newline gập space): {after}")

    def test_c14_11_backslash_badge_literal_via_lambda_repl(self):
        # PASS (documentation): patch_summary_live_rank dùng lambda repl
        # nên backslash trong badge không bị re.sub ăn làm escape.
        ws = Path(self._tmp)
        (ws / "SUMMARY.md").write_text(
            "- **Total Files Downloaded**: 0\n", encoding="utf-8")
        repo = WorkspaceRepo(str(ws))
        badge = r"- **Live Rank**: `\#1` `\back`"
        self.assertTrue(repo.patch_summary_live_rank(badge))
        self.assertIn(r"\back", (ws / "SUMMARY.md").read_text("utf-8"))


class TestSummaryPatchRace(unittest.TestCase):
    """Post-fix hunter-c14: patch_summary_live_rank đọc-sửa-ghi TRONG khóa
    ``<name>.lock`` và write_summary_md (regenerate) chia sẻ CÙNG khóa —
    hết cửa sổ RMW cho phép bản STALE của patcher đè summary mới."""

    def test_c14_12_patch_vs_generator_no_lost_update(self):
        # Trước fix (BUG-C14-4, M): cả hai writer dùng atomic_write_text
        # KHÔNG flock — cửa sổ đọc-sửa-viết khiến bản stale của patcher
        # ghi đè TOÀN BỘ SUMMARY vừa regenerate.
        tmp = tempfile.mkdtemp(prefix="hunter_c14_race_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ws = str(tmp)

        # v1: summary gốc có anchor files-line + hàng challenge cũ
        base = ("# Old\n"
                "- **Total Files Downloaded**: 0\n"
                "| OLD | table |\n")
        (Path(tmp) / "SUMMARY.md").write_text(base, encoding="utf-8")

        real_write = fileio.atomic_write_text
        read_done, gate = threading.Event(), threading.Event()

        def gated(path, text):
            # Chỉ chặn lần ghi của PATCHER (nhận diện qua nội dung Live Rank)
            if (os.path.basename(str(path)) == "SUMMARY.md"
                    and "**Live Rank**" in str(text)):
                read_done.set()
                gate.wait(timeout=5)
            return real_write(path, text)

        import ctf_downloader.storage.workspace_repo as repo_mod
        errors = []

        def patcher():
            try:
                with unittest.mock.patch.object(repo_mod, "atomic_write_text",
                                                gated):
                    ok = WorkspaceRepo(ws).patch_summary_live_rank(
                        "- **Live Rank**: `#1` / `9`")
                    if not ok:
                        errors.append(AssertionError("patch trả False"))
            except Exception as exc:
                errors.append(exc)

        th = threading.Thread(target=patcher)
        th.start()
        self.assertTrue(read_done.wait(timeout=5), "patcher chưa kịp đọc file")

        # Trong lúc patcher đã đọc xong nhưng CHƯA ghi: generator regenerate.
        # Post-fix generator ghi qua WorkspaceRepo.write_summary_md (cùng khóa
        # repo_mod) nên không cần mock riêng tầng generator nữa — bản mock
        # gen_mod.atomic_write_text cũ đã chết cùng refactor hunter-c14.
        info = CTFInfo(title="Fresh", url="https://x", challenges=[
            Challenge(id=99, name="NEWLY_GENERATED", category="Web",
                      points=7)])
        gen_errors = []

        def generator():
            try:
                SummaryGenerator.generate_summary(
                    base_output_dir=ws, ctf_info=info, all_results={})
            except Exception as exc:
                gen_errors.append(exc)

        th2 = threading.Thread(target=generator)
        th2.start(); th2.join(timeout=30)
        gate.set(); th.join(timeout=10)
        self.assertEqual([], errors + gen_errors,
                         "\n".join(traceback.format_exception(
                             type(e), e, e.__traceback__)
                             for e in errors + gen_errors))

        final = (Path(tmp) / "SUMMARY.md").read_text("utf-8")
        # Patch thành công về mặt API...
        self.assertIn("**Live Rank**", final)
        # ...nhưng nhấn chìm toàn bộ kết quả regenerate của generator:
        self.assertIn("NEWLY_GENERATED", final,
                      "lost update: bản stale của patcher ghi đè summary "
                      "vừa regenerate (RMW không khóa trên SUMMARY.md)")


# ======================================================================
# 3. HISTORY
# ======================================================================
class TestHistoryBoundaries(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hunter_c14_hist_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.ws = Path(self._tmp) / "ws"
        self.ws.mkdir()

    def _history(self, entries, show_all=False, extra=None):
        import json
        (self.ws / "submit_history.json").write_text(
            json.dumps({"entries": entries}), encoding="utf-8")
        from ctf_downloader.cli_commands import handle_history
        args = SimpleNamespace(workspace=str(self.ws), show_all=show_all)
        if extra:
            for k, v in extra.items():
                setattr(args, k, v)
        buf = io.StringIO()
        err = None
        try:
            with contextlib.redirect_stdout(buf):
                handle_history(args)
        except Exception as exc:
            err = exc
        return buf.getvalue(), err

    def test_c14_13_no_limit_flag_documented(self):
        # PASS (documentation): CLI history KHÔNG có --limit (cli.py chỉ định
        # -w/--all); handle_history luôn render TOÀN BỘ entries. Premise
        # "--limit 0/-1/>tổng" không tồn tại trên surface này.
        from ctf_downloader import cli
        # Kiểm chứng bằng source: section parser của history không định nghĩa
        # --limit; handler luôn render toàn bộ entries.
        src = inspect.getsource(cli)
        hist_section = src.split("'history'")[1].split("# 14.")[0]
        self.assertNotIn("--limit", hist_section)
        out, err = self._history([
            {"challenge_id": 1, "result": "correct", "flag": "PTIT{a}",
             "timestamp": "2026-01-01T00:00:00Z"},
        ] * 5, extra={"limit": 0})   # nếu ai truyền limit thì handler cũng bỏ qua
        self.assertIsNone(err)
        self.assertEqual(5, out.count("🚩✔"), "mọi entry đều được in")

    def test_c14_14_redact_flag_edges(self):
        # PASS (documentation): flag <4 ký tự bị lộ TOÀN BỘ ("abc***") —
        # giới hạn thiết kế của contract "4 ký tự đầu + ***".
        from ctf_downloader.cli_commands import _redact_flag
        self.assertEqual("-", _redact_flag(None))
        self.assertEqual("-", _redact_flag(""))
        self.assertEqual("abc***", _redact_flag("abc"))
        self.assertEqual("abcd***", _redact_flag("abcd"))
        self.assertEqual("ab***", _redact_flag("ab"))
        self.assertEqual("PTIT***", _redact_flag("PTIT{secret}"))

    def test_c14_15_weird_result_and_missing_fields(self):
        # PASS: result lạ -> icon ❓ + nguyên văn; entry rỗng -> '?', '-',
        # 'unknown' mà không crash.
        out, err = self._history([
            {"challenge_id": None, "result": "weird", "flag": "X"},
            {},
            {"challenge_id": 7, "result": None, "flag": None,
             "timestamp": None},
        ])
        self.assertIsNone(err)
        self.assertIn("❓ weird", out)
        self.assertIn("unknown", out)
        self.assertIn("?", out)
        self.assertIn("-", out)

    def test_c14_16_numeric_timestamp_does_not_crash(self):
        # EXPECTED FAIL (BUG-C14-5, M): timestamp dạng số (int epoch) — shape
        # mà status_service._solve_pulse chủ động hỗ trợ — đưa thẳng vào
        # print_table -> rich NotRenderableError CRASH cả lệnh `ctf history`.
        out, err = self._history([
            {"challenge_id": None, "result": "correct",
             "flag": "PTIT{x}", "timestamp": 1730000000},
        ])
        self.assertIsNone(err,
                         f"timestamp số làm history crash: {err!r}")

    def test_c14_17_print_table_nonstr_contract(self):
        # PASS (documentation contract): passthrough non-str của
        # Logger.print_table chỉ chấp nhận str/None/Rich renderable;
        # dict/list/int raise rich.errors.NotRenderableError lúc IN (không
        # lúc add_row). Call-site hiện tại đều str trừ timestamp (C14-16).
        from rich.errors import NotRenderableError
        from ctf_downloader.utils.logger import Logger
        for bad in ({"a": 1}, [1], 42):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf), \
                 self.assertRaises(NotRenderableError):
                Logger.print_table("T", ["X"], [[bad]])


# ======================================================================
# 4. SUMMARY GENERATOR
# ======================================================================
class TestSummaryGeneratorEdges(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hunter_c14_sum_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _gen(self, challs):
        path = SummaryGenerator.generate_summary(
            base_output_dir=self._tmp,
            ctf_info=CTFInfo(title="S", url="https://x", challenges=challs),
            all_results={})
        return Path(path).read_text("utf-8")

    def _broken_table_lines(self, text):
        """Mỗi block bảng markdown liền kề phải đồng nhất số cell (theo
        header của chính block đó) — phát hiện '|' / newline vỡ bảng bất
        kể bảng 3 hay 6 cột."""
        bad, block = [], []
        for line in text.splitlines() + [""]:
            if line.strip().startswith("|"):
                block.append(line)
                continue
            if block:
                ref = len(block[0].strip().split("|"))
                bad.extend(l for l in block
                           if len(l.strip().split("|")) != ref)
                block = []
        return bad

    def test_c14_18_category_pipe_newline_keeps_tables(self):
        # EXPECTED FAIL (BUG-C14-3, M): category chứa '|' / newline phá cả
        # bảng Categories Overview lẫn bảng chi tiết (_md_escape tồn tại ở
        # writeup_exporter nhưng generator không dùng helper nào).
        summary = self._gen([
            Challenge(id=1, name="n1", category="We|b", points=100),
            Challenge(id=2, name="n2", category="We\nb", points=100),
        ])
        self.assertEqual([], self._broken_table_lines(summary),
                         "bảng overview/chi tiết vỡ bởi '|' hoặc newline "
                         f"trong category: {self._broken_table_lines(summary)}")

    def test_c14_19_challenge_name_pipe_breaks_detail_table(self):
        # EXPECTED FAIL — cùng họ BUG-C14-3: tên challenge chứa '|' phá
        # hàng bảng 6 cột (test_summary_glyphs chỉ test tên sạch).
        summary = self._gen([
            Challenge(id=1, name="e|vil", category="Web", points=100),
        ])
        self.assertEqual([], self._broken_table_lines(summary),
                         "bảng chi tiết vỡ bởi '|' trong tên challenge: "
                         f"{self._broken_table_lines(summary)}")

    def test_c14_20_float_points_totals_consistent(self):
        # EXPECTED FAIL (BUG-C14-6, L): tổng điểm ép _safe_int (int() cắt
        # cụt 13.37 -> 13) trong khi hàng chi tiết hiển thị 13.37 — hai số
        # trái dấu nhau trên cùng một file. points=None in chữ "None" ra cell.
        summary = self._gen([
            Challenge(id=1, name="f", category="Web", points=13.37),
            Challenge(id=2, name="g", category="Web", points=None),
        ])
        self.assertIn("**Total Points Available**: 13.37", summary,
                      "tổng phải nhất quán với điểm hiển thị ở hàng (13.37)")
        self.assertNotIn("| None |", summary,
                         "points None không được in chữ 'None' ra cell")

    def test_c14_21_solved_glyph_with_writeup_none(self):
        # PASS (documentation): cột Status của SUMMARY chỉ phản ánh trục
        # solve (spec status-model); solved_by_me=True + writeup='none' vẫn
        # là "✔ Solved" — trục writeup không nằm trong SUMMARY (badge ✎ chỉ
        # ở dashboard/status tree).
        from ctf_downloader.services.status_service import ROW_GLYPHS
        summary = self._gen([
            Challenge(id=1, name="s1", category="Web", points=10,
                      solved_by_me=True),
            Challenge(id=2, name="s2", category="Web", points=10,
                      solved_by_me=False),
        ])
        g_ok = ROW_GLYPHS["solve"]["solved_by_me"][0]
        g_no = ROW_GLYPHS["solve"]["unsolved"][0]
        self.assertIn(f"| {g_ok} Solved |", summary)
        self.assertIn(f"| {g_no} Unsolved |", summary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
