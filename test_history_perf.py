"""PERF + TAIL — `ctf history` (audit hot-spot HS-A / HS-B, RESULTS.md):

1. PERF (HS-A): find_challenge từng bị gọi PER DISTINCT CID, mỗi lần rescan
   TOÀN workspace (300 cid x 300 metadata = 90k JSON parse, ~2.5-3.1s cho
   2000 entries). Fix: index snapshot MỘT lần (challenge_index()) rồi tra
   trên bộ nhớ — số lượt đọc metadata.json phải <= số file metadata trên đĩa,
   KHÔNG phụ thuộc số entry/cid distinct.
2. TAIL (HS-B): print_table rich 2000 rows ~400ms — mặc định chỉ render
   N entry mới nhất (--tail/--limit, default 100); caller dựng Namespace
   thủ công không có attr ``tail`` giữ nguyên hành vi cũ (render hết).
"""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.storage.workspace_repo import WorkspaceRepo


N_META = 6          # số metadata.json trên đĩa
N_DISTINCT = 12     # cid distinct trong history (một nửa không có trên đĩa)
N_ENTRIES = 48      # 4 entry/cid


class TestHistoryIndexOnce(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hist_perf_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.ws = os.path.join(self._tmp, "ws")
        os.makedirs(self.ws)
        for i in range(N_META):
            d = os.path.join(self.ws, f"chal-{i}")
            os.makedirs(d)
            with open(os.path.join(d, "metadata.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"id": i, "name": f"chal-name-{i}",
                           "category": "Web", "points": 100}, f)
        entries = [{"challenge_id": i % N_DISTINCT,
                    "result": "correct", "flag": f"F{{{i}}}",
                    "timestamp": "2026-01-01T00:00:00Z"}
                   for i in range(N_ENTRIES)]
        with open(os.path.join(self.ws, "submit_history.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"entries": entries}, f)

    def _run_counting_reads(self, **ns_extra):
        """Chạy handle_history đếm lượt read_metadata (proxy cho việc rescan
        đĩa). Trả (stdout, số lượt đọc)."""
        from ctf_downloader.cli_commands import handle_history

        real_read = WorkspaceRepo.read_metadata
        counter = {"n": 0}

        # wrapper gọn: đếm rồi gọi nguyên bản
        def counting(self_repo, path):
            counter["n"] += 1
            return real_read(self_repo, path)

        args = SimpleNamespace(workspace=self.ws, show_all=False, **ns_extra)
        buf = io.StringIO()
        with unittest.mock.patch.object(WorkspaceRepo, "read_metadata",
                                        counting), \
             contextlib.redirect_stdout(buf):
            handle_history(args)
        return buf.getvalue(), counter["n"]

    def test_c14_perf_read_metadata_bounded_by_disk_files(self):
        # RED trước fix: 12 cid distinct x 6 metadata = 72 lượt đọc.
        # GREEN sau fix: index build ĐÚNG 1 lần -> <= N_META lượt đọc.
        out, reads = self._run_counting_reads()
        self.assertLessEqual(
            reads, N_META,
            f"handle_history đọc metadata.json {reads} lần cho {N_META} file "
            f"— đang rescan đĩa theo cid thay vì index-once")
        # tính đúng vẫn giữ: cid có trên đĩa phải ra đúng tên
        self.assertIn("chal-name-0", out)
        self.assertIn("chal-name-5", out)

    def test_c14_perf_repeated_runs_still_bounded(self):
        _, r1 = self._run_counting_reads()
        _, r2 = self._run_counting_reads()
        self.assertEqual(r1, r2, "lần chạy thứ hai phải cùng chi phí")


class TestHistoryTailDefault(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hist_tail_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.ws = os.path.join(self._tmp, "ws")
        os.makedirs(self.ws)
        entries = [{"challenge_id": None, "result": "correct",
                    "flag": f"PTIT{{{i}}}", "timestamp": None}
                   for i in range(250)]
        with open(os.path.join(self.ws, "submit_history.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"entries": entries}, f)

    @staticmethod
    def _rows(out):
        return out.count("🚩✔")

    def test_default_tail_caps_render(self):
        # Namespace CÓ tail (như argparse default=100) -> chỉ 100 entry mới nhất.
        from ctf_downloader.cli_commands import handle_history
        args = SimpleNamespace(workspace=self.ws, show_all=False, tail=100)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            handle_history(args)
        self.assertEqual(100, self._rows(buf.getvalue()))

    def test_no_tail_attr_keeps_legacy_full_render(self):
        # Caller nội bộ dựng Namespace thủ công không có tail -> render hết
        # (backward-compat với test arch_phase7/ui_gaps hiện có).
        from ctf_downloader.cli_commands import handle_history
        args = SimpleNamespace(workspace=self.ws, show_all=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            handle_history(args)
        self.assertEqual(250, self._rows(buf.getvalue()))

    def test_show_all_and_nonpositive_lift_cap(self):
        from ctf_downloader.cli_commands import handle_history
        for extra in ({"show_all": True, "tail": 100},
                      {"show_all": False, "tail": 0}):
            args = SimpleNamespace(workspace=self.ws, **extra)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                handle_history(args)
            self.assertEqual(
                250, self._rows(buf.getvalue()),
                f"--all / tail<=0 phải bỏ cap: {extra}")

    def test_cli_parser_defines_tail_limit_alias(self):
        # Dựa trên parser THẬT (không parse source text — heuristic split
        # 'history' bắt nhầm section help TUI).
        import argparse as _ap
        from ctf_downloader.cli import build_unified_parser
        sub = next(a for a in build_unified_parser()._actions
                   if isinstance(a, _ap._SubParsersAction))
        hist = sub.choices['history']
        opts = {o for act in hist._actions for o in act.option_strings}
        self.assertIn("--tail", opts)
        self.assertIn("--limit", opts)
        tail_act = next(act for act in hist._actions
                        if getattr(act, "dest", None) == "tail")
        self.assertEqual(100, tail_act.default)


if __name__ == "__main__":
    unittest.main(verbosity=2)
