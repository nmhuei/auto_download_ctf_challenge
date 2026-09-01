"""Meter gradient cho status dashboard (ui.widgets.meter trong StatusService).

Chạy: python3 -m pytest test_status_meter.py -q
Kiểm tra:
- render_tree gọi ``meter`` với đúng value (% hoàn thành) và width.
- Output chứa glyph meter ▰/▱ (SPEC UI v2 §M1) ở cả hai path.
- Fallback plain ``plain_meter`` (không màu) cho non-TTY / terminal hẹp
  (<60 cols).
"""
import io
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from typing import List
from unittest.mock import patch

from rich.text import Text

from ctf_downloader.services import status_service
from ctf_downloader.services.status_service import StatusService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


class FakeTTY(io.StringIO):
    """Buffer bắt stdout nhưng giả lập TTY thật."""

    def isatty(self) -> bool:
        return True


def make_workspace(root: pathlib.Path):
    d = root / "Web" / "chall_a"
    d.mkdir(parents=True, exist_ok=True)
    (root / "challenges.json").write_text(json.dumps({
        "ctf_info": {"title": "StatusCTF", "platform": "gzctf"},
        "challenges": [{"id": 1, "name": "Chall A", "category": "Web", "points": 100}],
    }), encoding="utf-8")
    (d / "metadata.json").write_text(json.dumps({
        "id": 1, "name": "Chall A", "category": "Web", "points": 100,
        "solved_by_me": False,
    }), encoding="utf-8")
    return root


def fake_meter_recorder(calls: List[tuple]):
    """Thay ``ui.widgets.meter``: ghi lại (value, width) và trả Text đầy."""
    def _rec(value, width, colors, **kw):
        calls.append((value, width))
        return Text("▰" * max(width, 1))
    return _rec


class MeterTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="status_meter_")
        self.root = pathlib.Path(self._tmp) / "ws"
        self.root.mkdir()
        make_workspace(self.root)
        self.repo = WorkspaceRepo(self.root)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _render(self, buf):
        with redirect_stdout(buf):
            StatusService.render_tree(self.repo)


class TestMeterGradientPath(MeterTestCase):
    """TTY đủ rộng → meter được gọi với đúng value."""

    def test_meter_called_with_correct_values(self):
        # 1/1 solved → tổng 100% và category Web 100%
        self.repo.update_status(
            self.root / "Web" / "chall_a" / "metadata.json",
            lambda st: {**st, "solve": "solved_by_me"})
        calls: List[tuple] = []
        buf = FakeTTY()
        with patch.object(status_service.shutil, "get_terminal_size",
                          return_value=os.terminal_size((100, 24))), \
             patch("ctf_downloader.ui.widgets.meter", fake_meter_recorder(calls)), \
             patch("ctf_downloader.ui.widgets.gradient",
                   return_value=[(0, 200, 83)] * 101):
            self._render(buf)
        # PHOSPHOR redesign: bar tổng workspace chuyển vào cột TIẾN ĐỘ của
        # dashboard, meter gradient width 30 → 22 (category giữ nguyên 10).
        self.assertIn((100.0, 22), calls)   # bar tổng workspace (dashboard)
        self.assertIn((100.0, 10), calls)   # bar từng category
        out = buf.getvalue()
        self.assertIn("▰", out)             # output chứa glyph meter ▰


class TestFallbackPlain(MeterTestCase):
    """Non-TTY hoặc terminal hẹp (<60 cols) → fallback plain_meter, không
    gọi meter gradient."""

    def test_non_tty_falls_back_to_plain_bar(self):
        calls: List[tuple] = []
        buf = io.StringIO()  # không TTY
        with patch("ctf_downloader.ui.widgets.meter", fake_meter_recorder(calls)):
            self._render(buf)
        self.assertEqual(calls, [])         # meter KHÔNG được gọi
        out = buf.getvalue()
        self.assertIn("▱", out)             # glyph phần rỗng của plain_meter
        # PHOSPHOR: bar plain 22 ô nằm trong cột TIẾN ĐỘ của dashboard.
        self.assertIn("▱" * 22, out)
        self.assertIn("0/1", out)
        self.assertIn("0.0%", out)

    def test_narrow_tty_falls_back_to_plain_bar(self):
        calls: List[tuple] = []
        buf = FakeTTY()  # TTY nhưng <60 cols
        with patch.object(status_service.shutil, "get_terminal_size",
                          return_value=os.terminal_size((50, 24))), \
             patch("ctf_downloader.ui.widgets.meter", fake_meter_recorder(calls)):
            self._render(buf)
        self.assertEqual(calls, [])
        # PHOSPHOR: bar plain 22 ô nằm trong cột TIẾN ĐỘ của dashboard.
        self.assertIn("▱" * 22, buf.getvalue())
        self.assertIn("0.0%", buf.getvalue())

    def test_fallback_plain_is_colorless_and_ansi_safe(self):
        """Fallback ASCII-an-toàn: plain_meter — không span màu, không ANSI,
        đúng nguồn truth ``ui.widgets.plain_meter``."""
        from ctf_downloader.ui.widgets import plain_meter
        bar = StatusService._meter_only(40.0, 22)   # non-TTY → plain
        self.assertEqual(bar.plain, plain_meter(40.0, 22).plain)
        self.assertEqual(bar.spans, [])
        self.assertNotIn("\x1b", bar.plain)
        self.assertNotIn("#", bar.plain)


class TestSolveMeterRamp(MeterTestCase):
    """Solve progress uses a smooth per-cell semantic spectrum."""

    @staticmethod
    def _filled_styles(bar: Text):
        return [str(span.style) for span in bar.spans
                if span.style and str(span.style).startswith("#")]

    def test_ramp_is_smooth_multi_stop_spectrum(self):
        from ctf_downloader.services.status_service import _SOLVE_RAMP
        from ctf_downloader.ui.widgets import SOLVE_STOPS

        self.assertEqual(len(_SOLVE_RAMP), 101)
        self.assertEqual(_SOLVE_RAMP[0], SOLVE_STOPS[0])
        self.assertEqual(_SOLVE_RAMP[-1], SOLVE_STOPS[-1])
        self.assertGreaterEqual(len(set(_SOLVE_RAMP)), 95)
        for stop in SOLVE_STOPS:
            self.assertIn(stop, _SOLVE_RAMP)

    def test_18_cell_full_meter_has_distinct_progressive_colors(self):
        from ctf_downloader.services.status_service import _SOLVE_RAMP

        with patch.object(status_service.sys, "stdout", FakeTTY()), \
             patch.object(status_service.shutil, "get_terminal_size",
                          return_value=os.terminal_size((100, 24))):
            bar = StatusService._meter_only(100.0, 18)

        filled_styles = self._filled_styles(bar)
        allowed = {"#{:02x}{:02x}{:02x}".format(*rgb) for rgb in _SOLVE_RAMP}
        self.assertEqual(len(filled_styles), 18)
        self.assertGreaterEqual(len(set(filled_styles)), 15)
        self.assertTrue(set(filled_styles).issubset(allowed))
        self.assertEqual(bar.plain, "▰" * 18)

    def test_partial_meter_keeps_empty_cells_dim(self):
        with patch.object(status_service.sys, "stdout", FakeTTY()), \
             patch.object(status_service.shutil, "get_terminal_size",
                          return_value=os.terminal_size((100, 24))):
            bar = StatusService._meter_only(50.0, 18)

        self.assertIn("▱", bar.plain)
        empty_spans = [span for span in bar.spans if str(span.style) == "dim"]
        self.assertTrue(empty_spans)
        self.assertEqual(bar.plain.count("▰") + bar.plain.count("▱"), 18)


if __name__ == "__main__":
    unittest.main()
