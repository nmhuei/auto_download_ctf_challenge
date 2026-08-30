"""UI-ESCAPE: solver_names (dữ liệu SERVER) không được parse rich markup.

Hunter cycle-10: tên team/user lấy từ server được chèn thẳng vào cell của
``Logger.print_table`` — rich parse markup theo mặc định nên tên chứa
``[bold red]`` đổi style output, ``[/]`` lạc loài còn raise MarkupError
(crash bảng drift/sync). Điểm vá DUY NHẤT là render trung tâm
``logger.print_table``: cả 3 consumer (logger, cli_commands._render_verify_drift,
PullService.sync_workspace) đều build row rồi đi qua đây.

Hợp đồng: tên chứa markup phải hiện NGUYÊN VĂN như text thường (không đổi
style, không crash); tên bình thường giữ nguyên.
"""

import io
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from rich.console import Console

from ctf_downloader.ui import theme as ui_theme
from ctf_downloader.utils import logger as logger_mod


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text.replace("\x1b]\\", "]"))


def _render_table(rows, columns=("Ai solve",), title="T"):
    """Render bảng qua Logger.print_table với console thật (truecolor),
    trả về (ansi_output, plain_text)."""
    buf = io.StringIO()
    with patch.object(logger_mod, "console", Console(
            file=buf, width=200, force_terminal=True,
            color_system="truecolor", highlight=False,
            theme=ui_theme.load_theme(None))):
        with redirect_stdout(io.StringIO()):
            logger_mod.Logger.print_table(title, list(columns), rows)
    out = buf.getvalue()
    return out, _strip_ansi(out)


class TestPrintTableEscapesServerStrings(unittest.TestCase):
    def _render(self, rows, columns=("Ai solve",), title="T"):
        return _render_table(rows, columns, title)

    def test_markup_name_shows_verbatim_without_style_change(self):
        """Tên '[bold red]x[/]' phải hiện nguyên văn, KHÔNG được tô đỏ/đậm."""
        ansi, plain = self._render([["[bold red]x[/]"]])
        self.assertIn("[bold red]x[/]", plain)
        # Không SGR nào mở bold+đỏ ngay trước phần thân 'x' của tên.
        self.assertNotRegex(ansi, r"\x1b\[1;38;2;229;83;75m")

    def test_stray_close_tag_does_not_crash_and_shows_verbatim(self):
        ansi, plain = self._render([["team[/]name"]])
        self.assertIn("team[/]name", plain)
        self.assertNotRegex(ansi, r"\x1b\[[0-9;]*m\x1b\[0m")  # không nhảy style

    def test_link_tag_renders_as_plain_text(self):
        ansi, plain = self._render(
            [["[link=https://evil.example]click[/link]"]])
        self.assertIn("[link=https://evil.example]click[/link]", plain)
        self.assertNotIn("\x1b]8;", ansi)  # không phát hyperlink OSC 8

    def test_normal_names_unchanged(self):
        _, plain = self._render([["alice, bob"]])
        self.assertIn("alice, bob", plain)

    def test_drift_row_shape_like_real_consumers(self):
        """Dạng row thật từ pull_service/cli_commands: challenge name +
        category + solver_names join — toàn bộ cell server-data phải nguyên văn."""
        rows = [[f"[i]chal[/i] ([b]pwn[/b)", "team",
                 ", ".join(["[bold]t1[/]", "a[/]b"]) or "(không rõ)"]]
        _, plain = self._render(rows, ("Challenge", "By", "Solvers"))
        self.assertIn("[i]chal[/i] ([b]pwn[/b)", plain)
        self.assertIn("[bold]t1[/], a[/]b", plain)


class TestPrintTableEscapesTitleAndColumns(unittest.TestCase):
    """hunt-c20 LOW: title/columns cũng là dữ liệu (mode label, tên cột
    dựng từ chuỗi server) — rich Table parse markup trên chúng theo mặc định
    nên phải escape như cell, không style/crash."""

    def _render(self, rows, columns=("Ai solve",), title="T"):
        return _render_table(rows, columns, title)

    def test_title_with_markup_shows_verbatim(self):
        # Cột dài để bảng đủ rộng chứa title không bị wrap giữa các tag.
        _, plain = self._render(
            [["x"]], columns=("Rộng enough cho title dài hơn rất nhiều",),
            title="Sync [dim]drift[/] — ws[/]A")
        self.assertIn("Sync [dim]drift[/] — ws[/]A", plain)

    def test_columns_with_markup_show_verbatim(self):
        _, plain = self._render([["x"]], columns=("Chal[bold]i", "[/]By"))
        self.assertIn("Chal[bold]i", plain)
        self.assertIn("[/]By", plain)

    def test_plain_title_and_columns_unchanged(self):
        ansi, plain = self._render([["alice"]], columns=("By",), title="Sync")
        self.assertIn("Sync", plain)
        self.assertIn("By", plain)
        self.assertIn("alice", plain)
        # Không có SGR nào mở style quanh title/columns dữ liệu
        self.assertNotIn("\x1b[1mSync", ansi)


if __name__ == "__main__":
    unittest.main()
