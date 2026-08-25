"""Spec-audit fix — RANKING.md ghi qua WorkspaceRepo (atomic + flock).

Trước fix: rank_service._save_ranking_docs ghi RANKING.md bằng builtin
open() thô — bỏ qua repo layer, non-atomic, không flock (violation duy nhất
còn lại; mọi writer state khác đã đi qua storage/fileio helpers).

Kiểm tra:
- rank_service.py KHÔNG còn gọi builtin ``open()`` nào (AST check, cùng
  pattern test_arch_phase7.TestNoPromptInCliLayer) + phải đi qua
  ``WorkspaceRepo.write_ranking_md``.
- write_ranking_md nguyên tử: mock os.replace raise giữa chừng -> file cũ
  nguyên vẹn, không để lại rác .tmp; thành công thì dọn lockfile.
- Format RANKING.md GIỮ NGUYÊN byte-đối-byte so với bản trước refactor.
- Nhiều writer đồng thời: nội dung cuối luôn là MỘT bản ghi hoàn chỉnh.
"""
import ast
import datetime
import inspect
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

ROOT = os.path.dirname(os.path.abspath(__file__))
RANK_SERVICE = os.path.join(ROOT, "ctf_downloader", "services",
                            "rank_service.py")


class TestNoDirectOpenInRankService(unittest.TestCase):
    """rank_service là renderer/fetcher — KHÔNG tự ghi file bằng open().

    Mọi ghi state phải đi qua storage layer (WorkspaceRepo / fileio).
    """

    def _direct_open_calls(self, path):
        tree = ast.parse(open(path, encoding="utf-8").read())
        hits = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "open":
                hits.append(f"open() at line {node.lineno}")
        return hits

    def test_no_builtin_open_calls_in_rank_service(self):
        self.assertEqual([], self._direct_open_calls(RANK_SERVICE),
                         "rank_service không được gọi open() thô")

    def test_save_ranking_docs_goes_through_repo_write_ranking_md(self):
        from ctf_downloader.services.rank_service import RankService
        from ctf_downloader.storage.workspace_repo import WorkspaceRepo

        self.assertTrue(callable(getattr(WorkspaceRepo, "write_ranking_md",
                                         None)),
                        "WorkspaceRepo thiếu method write_ranking_md")
        src = inspect.getsource(RankService._save_ranking_docs)
        self.assertIn("write_ranking_md", src,
                      "_save_ranking_docs phải ghi qua repo.write_ranking_md")


class TestWriteRankingMdAtomic(unittest.TestCase):
    """WorkspaceRepo.write_ranking_md: atomic replace + giao thức lockfile
    nhất quán với locked_update_json."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rank_repo_")
        from ctf_downloader.storage.workspace_repo import WorkspaceRepo
        self.repo = WorkspaceRepo(self._tmp)
        self.target = Path(self._tmp) / "RANKING.md"
        self.target.write_text("OLD-RANKING", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_successful_write_replaces_content_and_cleans_lockfile(self):
        self.repo.write_ranking_md("NEW-RANKING")
        self.assertEqual("NEW-RANKING",
                         self.target.read_text(encoding="utf-8"))
        # Giao thức locked_update_json: lockfile unlink khi ghi THÀNH CÔNG,
        # không để lại tmp rác.
        self.assertFalse((Path(self._tmp) / "RANKING.md.lock").exists())
        leftovers = [p.name for p in Path(self._tmp).iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual([], leftovers)

    def test_replace_failure_keeps_old_file_intact_no_tmp_litter(self):
        import ctf_downloader.storage.fileio as fileio

        with patch.object(fileio.os, "replace",
                          side_effect=OSError(28, "ENOSPC")):
            with self.assertRaises(OSError):
                self.repo.write_ranking_md("BROKEN")
        # File cũ NGUYÊN VĂN — ghi hỏng giữa chừng không được phá dữ liệu.
        self.assertEqual("OLD-RANKING",
                         self.target.read_text(encoding="utf-8"))
        leftovers = [p.name for p in Path(self._tmp).iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual([], leftovers, "tmp hỏng phải được dọn")

    def test_symlink_target_is_resolved_and_written_through(self):
        real = Path(self._tmp) / "real.md"
        real.write_text("REAL-OLD", encoding="utf-8")
        link = Path(self._tmp) / "link.md"
        link.symlink_to(real)

        from ctf_downloader.storage.fileio import locked_write_text
        locked_write_text(link, "REAL-NEW")

        self.assertTrue(link.is_symlink(), "symlink không được thay bằng file thường")
        self.assertEqual("REAL-NEW", real.read_text(encoding="utf-8"))

    def test_concurrent_writers_never_interleave_content(self):
        """4 thread x 20 lần ghi trên cùng đích: nội dung cuối luôn là MỘT
        bản ghi hoàn chỉnh (atomic replace dưới flock), không rách/nửa vời."""
        errors = []

        def worker(t):
            try:
                for i in range(20):
                    self.repo.write_ranking_md(f"W{t}-{i}")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual([], errors)
        final = self.target.read_text(encoding="utf-8")
        self.assertRegex(final, r"^W\d+-\d+$", "bản ghi cuối phải hoàn chỉnh")


class TestRankingFormatPreserved(unittest.TestCase):
    """Format RANKING.md KHÔNG ĐỔI với user (byte-đối-byte với template cũ)
    và đường ghi mới vẫn gọi đủ: write_ranking_md + patch_summary_live_rank."""

    NOW_STR = "2026-08-25 12:00:00"

    DATA = {
        "title": "SuperCTF",
        "my_team": "ptit",
        "my_user": "huei",
        "my_rank": 2,
        "my_score": 1234,
        "total_teams": 10,
        "standings": [
            {"pos": 1, "name": "Alpha", "score": 2000},
            {"pos": 2, "name": "ptit", "score": 1234},
        ],
    }

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="rank_fmt_")
        from ctf_downloader.services import rank_service as rs
        from ctf_downloader.storage.workspace_repo import WorkspaceRepo

        svc = rs.RankService.__new__(rs.RankService)  # bỏ __init__ (network)
        svc.workspace_path = self._tmp
        svc.repo = WorkspaceRepo(self._tmp)

        fake_dt = MagicMock()
        fake_dt.datetime.now.return_value = datetime.datetime(
            2026, 8, 25, 12, 0, 0)
        with patch.object(rs, "datetime", fake_dt), \
             patch.object(WorkspaceRepo, "write_ranking_md",
                          wraps=svc.repo.write_ranking_md) as m_write, \
             patch.object(WorkspaceRepo, "patch_summary_live_rank",
                          return_value=False) as m_patch:
            svc._save_ranking_docs(self.DATA)

        self.m_write = m_write
        self.m_patch = m_patch
        self.content = (Path(self._tmp) / "RANKING.md").read_text(
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_writes_via_repo_once_and_patches_summary(self):
        self.m_write.assert_called_once()
        self.m_patch.assert_called_once()

    def test_ranking_md_bytes_identical_to_legacy_template(self):
        lines = [
            "# 🏆 Live Ranking & Scoreboard: SuperCTF\n",
            "- **Last Updated**: `2026-08-25 12:00:00`",
            "- **Team**: `ptit`",
            "- **User**: `huei`",
            "- **Current Rank**: `#2` / `10 teams`",
            "- **Total Points**: `1234 pts`\n",
            "## 📊 Top Standings\n",
            "| Rank | Team / Player | Points |",
            "| :---: | :--- | :---: |",
            "| #1 | Alpha | 2000 |",
            "| **#2** | **ptit (You)** 🎯 | **1234** |",
        ]
        lines.append("")
        expected = "\n".join(lines)
        self.assertEqual(expected, self.content)


if __name__ == "__main__":
    unittest.main()
