"""UI-TOKEN: Logger legacy ANSI phải được thay bằng phosphor tokens.

Live verify vòng 4 bắt `ctf rank` còn phát chrome cũ từ utils/logger.py:
bold cyan `[*]`, bold green `[+]`, vàng legacy `[!]`. Test này khóa token
mới theo PHOSPHOR FIELD KIT spec §3 (nguồn sự thật ui/theme.py):

- [*] info      → ACCENT_DEEP #6B4300 (amber tắt đèn — chrome faint)
- [+] success   → bold ACCENT #FFB000 (amber lead — chrome thuần, KHÔNG green)
- [!] warning   → WARN #EAC54F (warn amber)
- [-] error     → ERROR #E5534B (đỏ semantic)

Prefix ký tự `[*] [+] [!] [-]` phải giữ nguyên (nhiều test/caller assert).
"""

import io
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from rich.console import Console

from ctf_downloader.ui import theme as ui_theme
from ctf_downloader.utils import logger as logger_mod


def _rgb_seq(hex_color: str) -> str:
    """Đoạn tham số truecolor mà rich phát cho một mã hex (có thể gộp
    chung SGR với bold, ví dụ ``\\x1b[1;38;2;255;176;0m``)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"38;2;{r};{g};{b}m"


class TestLoggerPhosphorTokens(unittest.TestCase):
    def _render(self, method_name: str, msg: str) -> str:
        buf = io.StringIO()
        with patch.object(logger_mod, "console", Console(
                file=buf, width=120, force_terminal=True,
                color_system="truecolor", highlight=False,
                theme=ui_theme.load_theme(None))):
            with redirect_stdout(io.StringIO()):
                getattr(logger_mod.Logger, method_name)(msg)
        return buf.getvalue()

    def _strip_ansi(self, text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def test_info_uses_deep_amber_not_cyan(self):
        out = self._render("info", "Fetching leaderboard")
        self.assertNotIn("\x1b[1;36m", out)   # bold cyan legacy
        self.assertNotIn("[cyan", out)
        self.assertIn(_rgb_seq(ui_theme.ACCENT_DEEP), out)
        self.assertIn("[*] Fetching leaderboard", self._strip_ansi(out))

    def test_success_is_amber_lead_never_green(self):
        out = self._render("success", "Đã tải xong")
        for forbidden in ("\x1b[1;32m", "\x1b[32m"):  # bold green / green legacy
            self.assertNotIn(forbidden, out)
        self.assertIn(_rgb_seq(ui_theme.ACCENT), out)
        self.assertRegex(out, r"\x1b\[1(?:m|;)")  # giữ bold lead
        self.assertIn("[+] Đã tải xong", self._strip_ansi(out))

    def test_warning_uses_warn_amber_token(self):
        out = self._render("warning", "No standings data")
        for forbidden in ("\x1b[33m", "\x1b[1;33m"):  # vàng legacy
            self.assertNotIn(forbidden, out)
        self.assertIn(_rgb_seq(ui_theme.WARN), out)
        self.assertIn("[!] No standings data", self._strip_ansi(out))

    def test_error_uses_semantic_red(self):
        out = self._render("error", "Không lấy được ranking")
        self.assertNotIn("bright_red", out)
        self.assertIn(_rgb_seq(ui_theme.ERROR), out)
        self.assertIn("[-] Không lấy được ranking", self._strip_ansi(out))

    def test_console_theme_is_phosphor_source_of_truth(self):
        """Console logger resolve đúng token spec qua theme stack; alias
        magenta legacy ('highlight') đã biến mất."""
        import rich.errors
        from rich.color import Color

        for name, hex_color in (
                ("accent", ui_theme.ACCENT),
                ("warn", ui_theme.WARN),
                ("error", ui_theme.ERROR)):
            style = logger_mod.console.get_style(name)
            got = style.color.get_truecolor()
            want = Color.parse(hex_color).get_truecolor()
            self.assertEqual((got.red, got.green, got.blue),
                             (want.red, want.green, want.blue), name)
        with self.assertRaises(rich.errors.MissingStyle):
            logger_mod.console.get_style("highlight")


class TestLoggerMarkupContract(unittest.TestCase):
    """Follow-up C11-04: call-site CHỦ Ý trang trí bằng rich markup của tool
    truyền ``markup=True`` để màu quay lại; default ``markup=False`` vẫn
    escape tag đến từ dữ liệu server thành text NGUYÊN VĂN (không style)."""

    def _render(self, msg: str, markup: bool = False) -> str:
        buf = io.StringIO()
        with patch.object(logger_mod, "console", Console(
                file=buf, width=120, force_terminal=True,
                color_system="truecolor", highlight=False,
                theme=ui_theme.load_theme(None))):
            with redirect_stdout(io.StringIO()):
                logger_mod.Logger.success(msg, markup=markup)
        return buf.getvalue()

    def _strip_ansi(self, text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def test_markup_true_renders_color_and_consumes_tags(self):
        out = self._render("[bold green]ALL DONE[/bold green]", markup=True)
        self.assertNotIn("[bold green]", out)  # tag được parse, không in literal
        # bold green phát SGR thật quanh chữ
        self.assertRegex(out, r"\x1b\[(?:1;)?32mALL DONE\x1b\[0m")
        self.assertIn("[+] ALL DONE", self._strip_ansi(out))

    def test_default_false_keeps_escape_verbatim_without_style(self):
        out = self._render("[bold green]team[/]name</>")  # markup=False mặc định
        self.assertIn("[bold green]team[/]name</>", self._strip_ansi(out))
        # Sau chrome [+] không còn SGR nào: tag dữ liệu không biến thành màu.
        body = out.split("[+]\x1b[0m", 1)[1]
        self.assertNotRegex(body, r"\x1b\[")


if __name__ == "__main__":
    unittest.main()
