"""UIv2 synthesis #3 — mọi màu ANSI phải đi qua token Amber Refit.

Capture errtree_* bắt `ESC[1;32m`/`ESC[1;33m` (bold green/yellow legacy,
phụ thuộc theme terminal người dùng) ở dòng nhận diện platform cùng các
surface submit/wizard. File này khóa 3 module sở hữu
(platforms/detection.py, cli_legacy.py, services/submit_service.py):

- dòng nhận diện : label → ``[solved]`` + ✔; confidence high → solved+✔,
                   medium/low → ``[warn]`` + ! (glyph đi kèm màu — spec §3)
- title wizard   → ``[title]`` (amber lead), phím chọn menu → ``[hi_fg]``
- dữ liệu (tên challenge, path, flag, regex literal) → ``[info]``/``[literal]``
  neutral — không còn tag màu legacy nào trong nguồn.
"""

import io
import re
import unittest
import urllib.parse
from contextlib import redirect_stdout
from unittest.mock import patch

from rich.console import Console

from ctf_downloader.platforms import detection
from ctf_downloader.ui import theme as ui_theme
from ctf_downloader.utils import logger as logger_mod


def _rgb_seq(hex_color: str) -> str:
    """Tham số truecolor rich phát cho một mã hex token."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"38;2;{r};{g};{b}m"


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class FakeResponse:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


class RoutingSession:
    """Session giả định tuyến theo path (mặc định 404) — không mạng."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        path = urllib.parse.urlparse(url).path or "/"
        resp = self.routes.get(path, self.routes.get("*"))
        if resp is None:
            return FakeResponse(status_code=404, text="Not Found")
        return resp


class TestDetectionTokenLine(unittest.TestCase):
    """Dòng nhận diện platform phát đúng token + glyph, hết SGR legacy."""

    LEGACY_SGR = ("\x1b[1;32m", "\x1b[32m", "\x1b[1;33m", "\x1b[33m")

    def _render_detection(self, url, routes=None):
        buf = io.StringIO()
        with patch.object(logger_mod, "console", Console(
                file=buf, width=120, force_terminal=True,
                color_system="truecolor", highlight=False,
                theme=ui_theme.load_theme(None))):
            with redirect_stdout(io.StringIO()):
                detection.detect_platform_info(url, RoutingSession(routes))
        return buf.getvalue()

    def test_high_confidence_solved_token_with_check_glyph(self):
        out = self._render_detection(
            "https://gz.example.com/",
            {"*": FakeResponse(200, "<html>GZ::CTF scoreboard</html>")})
        for forbidden in self.LEGACY_SGR:
            self.assertNotIn(forbidden, out)
        self.assertIn(_rgb_seq(ui_theme.SOLVED), out)
        plain = _strip_ansi(out)
        self.assertIn("Nhận diện platform: ✔ GZ::CTF", plain)
        self.assertIn("(độ tin cậy: ✔ high)", plain)

    def test_medium_confidence_warn_token_with_bang_glyph(self):
        # Không marker/probe nào khớp, URL chứa /games -> gzctf confidence
        # medium (hành vi cũ tầng 4) — nhánh warn amber của dòng nhận diện.
        out = self._render_detection("https://mystery.example.com/games/6")
        for forbidden in self.LEGACY_SGR:
            self.assertNotIn(forbidden, out)
        self.assertIn(_rgb_seq(ui_theme.WARN), out)
        plain = _strip_ansi(out)
        self.assertIn("(độ tin cậy: ! medium)", plain)


class TestNoLegacyColorTagsInOwnedModules(unittest.TestCase):
    """Khóa nguồn: 3 module sở hữu không còn tag màu legacy / SGR tay."""

    SOURCES = (
        "ctf_downloader/platforms/detection.py",
        "ctf_downloader/cli_legacy.py",
        "ctf_downloader/services/submit_service.py",
    )
    FORBIDDEN_TAGS = ("[bold green]", "[bold yellow]", "[bold cyan]",
                      "[green]", "[yellow]", "[cyan]", "[red]")

    @staticmethod
    def _src(rel):
        with open(rel, encoding="utf-8") as fh:
            return fh.read()

    def test_no_legacy_tags_or_raw_sgr(self):
        for rel in self.SOURCES:
            src = self._src(rel)
            for tag in self.FORBIDDEN_TAGS:
                self.assertNotIn(tag, src, f"{rel}: {tag}")
            self.assertNotIn("\x1b[", src, rel)

    def test_detection_line_uses_solved_token_and_glyph(self):
        src = self._src("ctf_downloader/platforms/detection.py")
        self.assertIn("[solved]", src)
        self.assertIn("{conf_glyph}", src)

    def test_wizard_uses_title_and_hi_fg_tokens(self):
        src = self._src("ctf_downloader/cli_legacy.py")
        self.assertIn("[title]🚩 Interactive Flag Submitter[/title]", src)
        self.assertIn("[hi_fg]1[/hi_fg]", src)

    def test_submit_service_data_uses_neutral_tokens(self):
        src = self._src("ctf_downloader/services/submit_service.py")
        # Regex format là literal; tên/path/flag là dữ liệu → neutral.
        self.assertIn("[literal]{escape(fmt)}[/literal]", src)
        # hunt-c18: +1 là dòng "challenge đã solved — bỏ qua candidate còn
        # lại" trong auto_scan_and_submit (tên challenge = dữ liệu → [info]).
        self.assertEqual(src.count("[info]"), 6)


if __name__ == "__main__":
    unittest.main()
