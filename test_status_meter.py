"""Meter gradient cho status dashboard (ui.widgets.meter trong StatusService).

Chạy: python3 -m pytest test_status_meter.py -q
Kiểm tra:
- render_tree gọi ``meter`` với đúng value (% hoàn thành) và width.
- Output chứa ký tự block ('█'/'░') ở cả hai path.
- Fallback plain cho non-TTY / terminal hẹp (<60 cols).
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
        return Text("█" * max(width, 1))
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
        self.assertIn("█", out)             # output chứa ký tự block


class TestFallbackPlain(MeterTestCase):
    """Non-TTY hoặc terminal hẹp (<60 cols) → fallback bar cũ, không gọi meter."""

    def test_non_tty_falls_back_to_plain_bar(self):
        calls: List[tuple] = []
        buf = io.StringIO()  # không TTY
        with patch("ctf_downloader.ui.widgets.meter", fake_meter_recorder(calls)):
            self._render(buf)
        self.assertEqual(calls, [])         # meter KHÔNG được gọi
        out = buf.getvalue()
        self.assertIn("░", out)             # ký tự block phần rỗng
        # PHOSPHOR: bar plain 22 ô nằm trong cột TIẾN ĐỘ của dashboard.
        self.assertIn("░" * 22, out)
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
        self.assertIn("░" * 22, buf.getvalue())
        self.assertIn("0.0%", buf.getvalue())


class TestPhosphorMeterRamp(MeterTestCase):
    """codex-r3 #1: meter chỉ dùng ĐÚNG 3 mốc màu spec §3.3 — không còn
    các bước nội suy trung gian (#885800/#FFD97B cũ)."""

    SPEC_HEXES = {"#6b4300", "#ffb000", "#ffe49a"}

    def test_ramp_constant_is_exactly_three_spec_stops(self):
        from ctf_downloader.services.status_service import _METER_RAMP_3STOP
        self.assertEqual(set(_METER_RAMP_3STOP), {
            (0x6B, 0x43, 0x00), (0xFF, 0xB0, 0x00), (0xFF, 0xE4, 0x9A)})
        self.assertEqual(len(_METER_RAMP_3STOP), 101)

    def test_rendered_meter_cells_use_only_spec_colors(self):
        bar = StatusService._meter_only(100.0, 30)  # non-TTY → plain
        self.assertNotIn("#", bar.plain)
        with patch.object(status_service.sys, "stdout", FakeTTY()), \
             patch.object(status_service.shutil, "get_terminal_size",
                          return_value=os.terminal_size((100, 24))):
            bar = StatusService._meter_only(73.0, 30)
        filled_styles = {s.style for s in bar.spans
                         if s.style and str(s.style).startswith("#")}
        self.assertEqual(filled_styles, self.SPEC_HEXES,
                         f"meter lộ màu ngoài 3 mốc: {filled_styles}")


if __name__ == "__main__":
    unittest.main()
