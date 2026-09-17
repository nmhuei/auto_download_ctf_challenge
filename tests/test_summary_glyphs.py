"""Spec-audit GAP (L): glyph cột Status trong SUMMARY.md phải theo bộ chung
spec challenge-status-model §6 — tầng phosphor ``ROW_GLYPHS``
(services/status_service.py): ``✔`` solved · ``·`` unsolved.

Trước fix generator dùng emoji tự chọn lệch hệ ("✅ Solved" / "⏳ Unsolved").
"""
import os
import shutil
import tempfile
import unittest

from ctf_downloader.generator.summary_generator import SummaryGenerator
from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.services.status_service import ROW_GLYPHS


class TestSummaryGlyphs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="summary_glyph_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)

    def _generate(self, challs):
        info = CTFInfo(title="GlyphCTF", url="https://x.example",
                       challenges=challs)
        path = SummaryGenerator.generate_summary(
            base_output_dir=self._tmp, ctf_info=info,
            all_results={c.id: [] for c in challs},
        )
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_solved_row_uses_rowglyphs_check(self):
        summary = self._generate([
            Challenge(id=1, name="WebDog", category="Web", points=100,
                      solved_by_me=True),
            Challenge(id=2, name="PwnCat", category="Pwn", points=200,
                      solved_by_me=False),
        ])
        solved_glyph = ROW_GLYPHS["solve"]["solved_by_me"][0]
        unsolved_glyph = ROW_GLYPHS["solve"]["unsolved"][0]
        self.assertIn(f"| {solved_glyph} Solved |", summary)
        self.assertIn(f"| {unsolved_glyph} Unsolved |", summary)

    def test_offsystem_emoji_removed(self):
        summary = self._generate([
            Challenge(id=1, name="A", category="Web", solved_by_me=True),
            Challenge(id=2, name="B", category="Web", solved_by_me=False),
        ])
        self.assertNotIn("✅", summary)
        self.assertNotIn("⏳", summary)

    def test_table_integrity_six_columns(self):
        """Glyph là text thuần trong cell — không phá bảng markdown."""
        summary = self._generate([
            Challenge(id=1, name="PipeFree", category="Web", solved_by_me=True),
        ])
        for line in summary.splitlines():
            if line.startswith("| **["):
                cells = [c for c in line.split("|")]
                # leading + trailing pipe -> 8 mảnh, 6 cell thật
                self.assertEqual(len(cells), 8, line)
                self.assertTrue(line.endswith("|"), line)
                # [-1] = đuôi rỗng, [-2] = cell Path, [-3] = cell Status
                self.assertEqual(cells[-3].strip(),
                                 f"{ROW_GLYPHS['solve']['solved_by_me'][0]} Solved")


if __name__ == "__main__":
    unittest.main()
