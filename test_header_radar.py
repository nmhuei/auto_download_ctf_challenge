"""Header swap combo B — Phosphor Radar: scanline full-width + title căn giữa.

Ràng buộc: chiều cao ≤ 4 dòng (spec §4.1), tự căn theo ``width`` không
hardcode, mọi màu lấy từ token :mod:`ctf_downloader.ui.theme`.
"""
from __future__ import annotations

import unittest

from rich.cells import cell_len

from ctf_downloader.ui.banner import app_header


class TestPhosphorRadarHeader(unittest.TestCase):
    """app_header() kiểu radar — hợp đồng hình học + payload."""

    def test_height_exactly_4_at_width_80_and_120(self):
        for w in (80, 120):
            with self.subTest(width=w):
                t = app_header("status", context="ws:PTIT-CTF-2026",
                               timestamp="10:13 UTC+7", width=w)
                lines = t.plain.splitlines()
                self.assertEqual(4, len(lines), f"w={w}: {lines!r}")

    def test_every_line_fits_width_80_and_120(self):
        for w in (80, 120):
            with self.subTest(width=w):
                t = app_header("status", context="ws:X",
                               timestamp="10:13 UTC+7", width=w)
                for ln in t.plain.splitlines():
                    self.assertLessEqual(cell_len(ln), w, repr(ln))

    def test_line1_is_fullwidth_scanline_ramp(self):
        t = app_header("status", timestamp="10:13", width=80)
        first = t.plain.splitlines()[0]
        self.assertTrue(first, "dòng 1 rỗng")
        self.assertLessEqual(cell_len(first), 80)
        self.assertTrue(set("░▒▓") <= set(first),
                        f"dòng 1 thiếu glyph ramp: {first!r}")
        # full-width texture: không cell trống nào trên dải scanline.
        self.assertNotIn(" ", first)

    def test_title_brand_centered_between_dot_wings(self):
        t = app_header("status", timestamp="10:13", width=80)
        line2 = t.plain.splitlines()[1]
        self.assertIn("CTF·TOOLKIT", line2)
        self.assertIn("v3", line2)
        left = len(line2) - len(line2.lstrip("·"))
        right = len(line2) - len(line2.rstrip("·"))
        self.assertLessEqual(abs(left - right), 2,
                             f"title lệch tâm: {line2!r}")

    def test_payload_command_context_timestamp_present(self):
        plain = app_header("sync", context="ws:PTIT",
                           timestamp="10:13 UTC+7").plain
        for piece in ("sync", "ws:PTIT", "10:13 UTC+7"):
            self.assertIn(piece, plain)

    def test_no_timestamp_no_context_stays_under_4_lines(self):
        t = app_header("pull", width=80)
        self.assertLessEqual(len(t.plain.splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
