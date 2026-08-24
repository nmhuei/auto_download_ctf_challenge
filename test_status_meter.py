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
        self.assertIn((100.0, 30), calls)   # bar tổng workspace
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
        # Bar tổng 30 ô với 0% tiến độ: toàn ░
        self.assertIn("[" + "░" * 30 + "] 0.0%", out)

    def test_narrow_tty_falls_back_to_plain_bar(self):
        calls: List[tuple] = []
        buf = FakeTTY()  # TTY nhưng <60 cols
        with patch.object(status_service.shutil, "get_terminal_size",
                          return_value=os.terminal_size((50, 24))), \
             patch("ctf_downloader.ui.widgets.meter", fake_meter_recorder(calls)):
            self._render(buf)
        self.assertEqual(calls, [])
        self.assertIn("[" + "░" * 30 + "] 0.0%", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
