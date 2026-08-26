"""Review-5 follow-up (verdict APPROVE-WITH-FINDINGS trên commit f517e05).

4 finding phải fix:

1. M · rank_service._save_ranking_docs: rank_badge nhúng ``my_team`` THÔ
   rồi patch_summary_live_rank chèn NGUYÊN VĂN vào SUMMARY.md —
   ANSI/newline/pipe từ tên team lọt qua đường badge dù RANKING.md đã
   escape (trái mục tiêu BUG-C14-2).
2. L · sanitize.md_cell chưa thoát ``[``/``]`` — link-text
   ``[md_cell(name)](rel_path)`` ở summary_generator và dòng standings ở
   rank_service vẫn vỡ link / markdown injection khi tên chứa ngoặc.
3. L · unify: hai chiến lược escape markdown song song
   (writeup_exporter._md_escape vs sanitize.md_cell) gộp về MỘT
   implementation dùng chung trong sanitize; hành vi output cả hai caller
   KHÔNG ĐỔI.
4. L cosmetic · summary_generator tổng điểm float in artefact
   (0.1 + 0.2 -> ``0.30000000000000004``).

TDD: các test dưới đây viết TRƯỚC khi fix — chạy RED đúng lý do, sau fix
phải GREEN toàn bộ cùng suite cũ.
"""
import os
import re
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.generator.summary_generator import SummaryGenerator
from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.services import rank_service as rs
from ctf_downloader.services import writeup_exporter as we
from ctf_downloader.services.rank_service import RankService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo
from ctf_downloader.utils.sanitize import escape_markdown, md_cell


def make_svc(workspace):
    """RankService bỏ __init__ (không network/session/detector)."""
    svc = RankService.__new__(RankService)
    svc.workspace_path = workspace
    svc.repo = WorkspaceRepo(workspace) if workspace else None
    return svc


# ======================================================================
# 1. M — badge Live Rank phải escape my_team trước khi vào SUMMARY.md
# ======================================================================
class TestRankBadgeTeamEscape(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="review5_badge_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _save(self, my_team):
        ws = Path(self._tmp)
        (ws / "SUMMARY.md").write_text(
            "# Summary\n- **Total Files Downloaded**: 0\n", encoding="utf-8")
        data = {"title": "CTF", "my_team": my_team, "my_user": "-",
                "my_rank": 1, "my_score": 10, "total_teams": 2,
                "standings": [{"pos": 1, "name": "someone-else",
                               "score": 10}]}
        make_svc(str(ws))._save_ranking_docs(data)
        return (ws / "SUMMARY.md").read_text(encoding="utf-8")

    def _live_lines(self, summary):
        return [l for l in summary.splitlines()
                if l.startswith("- **Live Rank**")]

    def test_badge_ansi_newline_pipe_never_reach_summary(self):
        # Tên team do SERVER kiểm soát: ESC đổi màu + newline sinh dòng giả
        # + pipe. Badge chèn nguyên văn nên cả ba phải được trung hoà trước.
        evil = "\x1b[31mE|VIL\nINJ"
        summary = self._save(evil)

        self.assertNotIn("\x1b", summary,
                         "ESC từ tên team lọt nguyên vào SUMMARY.md "
                         "(đường badge chưa strip_ansi)")
        live = self._live_lines(summary)
        self.assertEqual(1, len(live),
                         f"newline trong team name sinh dòng giả qua badge: "
                         f"{summary.splitlines()}")
        # pipe -> thực thể HTML như hợp đồng md_cell; newline gập space.
        self.assertIn("(Team: `E&#124;VIL INJ`)", live[0],
                      f"badge không qua md_cell: {live}")

    def test_badge_brackets_in_team_name_escaped(self):
        summary = self._save("Te[am](http://evil)")
        self.assertNotIn("[am](http://evil)", summary,
                         "markdown link injection qua team name trong badge")
        self.assertIn("(Team: `Te\\[am\\](http://evil)`)", summary)

    def test_clean_team_name_badge_byte_identical(self):
        # Backward-compat: tên sạch đi qua NGUYÊN VẰN (không escape thừa).
        summary = self._save("team_x")
        self.assertIn("- **Live Rank**: `#1` / `2` (Team: `team_x`)",
                      self._live_lines(summary)[0])


# ======================================================================
# 2. L — md_cell phải thoát '[' ']' (link-text + standings row)
# ======================================================================
class TestMdCellBracketEscape(unittest.TestCase):
    def test_md_cell_escapes_brackets(self):
        self.assertEqual(r"\[Link\](x)", md_cell("[Link](x)"))

    def test_md_cell_contract_unchanged_for_non_bracket_input(self):
        # Hợp đồng cũ giữ nguyên: sạch no-op, pipe->entity, newline gập,
        # ANSI strip.
        self.assertEqual("Easy SQLi", md_cell("Easy SQLi"))
        self.assertEqual("a&#124;b", md_cell("a|b"))
        self.assertEqual("E VIL", md_cell("\x1b[31mE\nVIL"))
        self.assertEqual("", md_cell(None))

    def test_summary_link_text_with_brackets_stays_one_link(self):
        tmp = tempfile.mkdtemp(prefix="review5_sum_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        summary = SummaryGenerator.generate_summary(
            base_output_dir=tmp,
            ctf_info=CTFInfo(title="S", url="https://x", challenges=[
                Challenge(id=1, name="Evil [x](http://evil)",
                          category="Web", points=100),
            ]),
            all_results={})
        text = Path(summary).read_text(encoding="utf-8")
        # Trước fix: "[Evil [x](http://evil)](path)" — ngoặc lồng vỡ cấu
        # trúc link + markdown injection. Sau fix ngoặc phải escaped.
        self.assertNotIn("[x](http://evil)", text,
                         "link injection qua tên challenge chưa được escape")
        self.assertIn("\\[x\\]", text)
        # Bảng 6 cột không vỡ: hàng challenge có đúng 8 mảnh '|'.
        row = next(l for l in text.splitlines() if "\\[x\\]" in l)
        self.assertEqual(8, len(row.split("|")), row)

    def test_rank_standings_row_with_brackets_escaped(self):
        tmp = tempfile.mkdtemp(prefix="review5_rank_row_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        data = {"title": "CTF", "my_team": "-", "my_user": "-",
                "my_rank": 2, "my_score": 5, "total_teams": 2,
                "standings": [
                    {"pos": 1, "name": "A[B]C", "score": 10},
                    {"pos": 2, "name": "-", "score": 5},
                ]}
        make_svc(tmp)._save_ranking_docs(data)
        ranking = (Path(tmp) / "RANKING.md").read_text(encoding="utf-8")
        row = next(l for l in ranking.splitlines() if "A" in l
                   and l.startswith("| #"))
        self.assertNotIn("A[B]C", row,
                         "ngoặc vuông từ tên team vỡ markdown hàng bảng")
        self.assertIn("A\\[B\\]C", row)


# ======================================================================
# 3. L — MỘT implementation escape dùng chung (sanitize)
# ======================================================================
class TestUnifiedMarkdownEscape(unittest.TestCase):
    ALL_SPECIALS = "a_b*c[d]e`f\\g|h"

    def test_sanitize_exposes_shared_escape_markdown(self):
        raw = self.ALL_SPECIALS
        expected = re.sub(r"([\\`*_\[\]|])", r"\\\1", raw)
        self.assertEqual(expected, escape_markdown(raw))

    def test_exporter_md_escape_delegates_to_sanitize(self):
        # _md_escape phải là thin delegate — patch implementation chung phải
        # thấy được từ phía exporter (chứng minh MỘT nguồn).
        with unittest.mock.patch.object(
                we, "escape_markdown", return_value="X") as mock_esc:
            self.assertEqual("X", we._md_escape("anything"))
            mock_esc.assert_called_once_with("anything")

    def test_exporter_output_behavior_unchanged(self):
        # Toàn bộ ký tự đặc biệt cũ vẫn backslash-escape y hệt.
        raw = self.ALL_SPECIALS + " Mi[sc] *bold* `code` \\ path | end"
        expected = re.sub(r"([\\`*_\[\]|])", r"\\\1", raw)
        self.assertEqual(expected, we._md_escape(raw))
        self.assertNotIn("[sc]", we._md_escape("Mi[sc]"))

    def test_md_cell_routes_through_same_table(self):
        # md_cell cũng đi qua bảng escape chung (subset '[]' — pipe xử lý
        # riêng bằng thực thể vì backslash-escape không đủ trong ô bảng).
        with unittest.mock.patch.object(
                rs, "md_cell", wraps=md_cell):
            pass  # smoke: import path rank_service dùng md_cell từ sanitize
        self.assertEqual(r"\[o\]", md_cell("[o]"))


# ======================================================================
# 4. L cosmetic — tổng điểm không in artefact float
# ======================================================================
class TestTotalPointsFloatArtifact(unittest.TestCase):
    def _gen(self, challs):
        tmp = tempfile.mkdtemp(prefix="review5_float_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = SummaryGenerator.generate_summary(
            base_output_dir=tmp,
            ctf_info=CTFInfo(title="F", url="https://x", challenges=challs),
            all_results={})
        return Path(path).read_text(encoding="utf-8")

    def test_total_no_binary_float_artifact(self):
        summary = self._gen([
            Challenge(id=1, name="a", category="Web", points=0.1),
            Challenge(id=2, name="b", category="Web", points=0.2),
        ])
        self.assertNotIn("0.30000000000000004", summary,
                         "artefact cộng float in thẳng ra SUMMARY.md")
        self.assertIn("**Total Points Available**: 0.3", summary)

    def test_category_total_same_treatment(self):
        summary = self._gen([
            Challenge(id=1, name="a", category="Web", points=0.1),
            Challenge(id=2, name="b", category="Web", points=0.2),
        ])
        self.assertRegex(summary, r"\| \*\*Web\*\* \| 2 \| 0\.3 \|")

    def test_int_and_decimal_totals_unchanged(self):
        summary = self._gen([
            Challenge(id=1, name="i", category="Pwn", points=100),
            Challenge(id=2, name="d", category="Crypto", points=13.37),
        ])
        self.assertIn("**Total Points Available**: 113.37", summary)
        self.assertRegex(summary, r"\| \*\*Pwn\*\* \| 1 \| 100 \|")


if __name__ == "__main__":
    unittest.main(verbosity=2)
