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


class TestPrintTableEscapesServerStrings(unittest.TestCase):
    def _render(self, rows, columns=("Ai solve",)):
        """Render bảng qua Logger.print_table với console thật (truecolor),
        trả về (ansi_output, plain_text)."""
        buf = io.StringIO()
        with patch.object(logger_mod, "console", Console(
                file=buf, width=200, force_terminal=True,
                color_system="truecolor", highlight=False,
                theme=ui_theme.load_theme(None))):
            with redirect_stdout(io.StringIO()):
                logger_mod.Logger.print_table("T", list(columns), rows)
        out = buf.getvalue()
        return out, _strip_ansi(out)

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


if __name__ == "__main__":
    unittest.main()
