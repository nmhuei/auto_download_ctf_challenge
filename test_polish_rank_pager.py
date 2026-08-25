"""Polish pass (integration verifier notes #1 + #2):

1. Rank i18n residual — các dòng EN còn sót giữa output tiếng Việt:
   - ``Detected Platform: ...`` (platforms/detection.py)
   - ``Fetching live leaderboard and ranking ...`` (services/rank_service.py)
   - ``Updated live ranking document: ...``  (services/rank_service.py)
2. Pager footer gate — footer keybinding (chrome) KHÔNG in khi stdout
   non-tty (uv-style: pipe → machine-readable, không chrome). Gate đặt ở
   ``ui.widgets.footer_bar`` (mặc định theo ``sys.stdout.isatty()``, caller
   TUI luôn-tương-tác truyền ``tty=True``).
"""
import contextlib
import io
import inspect
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


class FakeTTY(io.StringIO):
    """Buffer giả lập terminal: ``isatty()`` trả True."""

    def isatty(self):
        return True


class RankI18nTests(unittest.TestCase):
    """Note #1: không còn dòng EN trộn trong output rank tiếng Việt."""

    def _svc(self, tmp):
        from ctf_downloader.services.rank_service import RankService

        with patch("ctf_downloader.services.rank_service.create_session",
                   return_value=MagicMock()), \
             patch("ctf_downloader.services.rank_service"
                   ".PlatformDetector.detect_platform",
                   return_value=MagicMock()):
            return RankService(workspace_path=tmp,
                               url="https://ctf.example.test")

    def _capture_logs(self, fn, *a, **kw):
        logs = []
        with patch("ctf_downloader.services.rank_service.Logger") as logger:
            logger.info.side_effect = (
                lambda msg, *x, **y: logs.append(str(msg)))
            fn(*a, **kw)
        return logs

    def test_fetch_ranking_logs_vietnamese(self):
        svc = self._svc(tempfile.mkdtemp())
        svc.platform.authenticate = MagicMock(return_value=True)
        svc.platform.fetch_scoreboard = MagicMock(
            return_value={"standings": [], "title": "X"})

        logs = self._capture_logs(svc.fetch_ranking)

        self.assertTrue(logs, "fetch_ranking phải log 1 dòng trạng thái")
        joined = "\n".join(logs)
        self.assertIn("Đang tải bảng xếp hạng", joined)
        for en in ("Fetching live leaderboard", "ranking from CTF platform"):
            self.assertNotIn(en, joined)

    def test_save_ranking_docs_logs_vietnamese(self):
        tmp = tempfile.mkdtemp()
        svc = self._svc(tmp)
        data = {"title": "CTF Test", "standings": [
            {"pos": 1, "name": "TeamA", "score": 100}],
            "my_team": "TeamA", "my_rank": 1, "my_score": 100,
            "total_teams": 1}

        logs = self._capture_logs(svc._save_ranking_docs, data)

        joined = "\n".join(logs)
        self.assertIn("Đã cập nhật bảng xếp hạng live", joined)
        self.assertIn("RANKING.md", joined)
        self.assertNotIn("Updated live ranking document", joined)
        self.assertTrue(os.path.exists(os.path.join(tmp, "RANKING.md")))

    def test_no_standings_warning_vietnamese(self):
        svc = self._svc(tempfile.mkdtemp())
        svc.platform.authenticate = MagicMock(return_value=True)
        svc.platform.fetch_scoreboard = MagicMock(
            return_value={"standings": [], "title": "X"})
        warnings = []
        with patch("ctf_downloader.services.rank_service.Logger") as logger:
            logger.info.side_effect = lambda m, *a, **k: None
            logger.warning.side_effect = (
                lambda m, *a, **k: warnings.append(str(m)))
            svc.display_and_update(update_docs=False)

        joined = "\n".join(warnings)
        self.assertIn("chưa có dữ liệu standings", joined)
        self.assertNotIn("No standings data available", joined)

    def test_detection_message_translated_at_source(self):
        # detect_platform_info cần HTTP thật để probe → kiểm tra tại NGUỒN:
        # chuỗi user-facing đã dịch, token markup giữ nguyên.
        from ctf_downloader.platforms import detection

        src = inspect.getsource(detection.detect_platform_info)
        self.assertIn('"Nhận diện platform:', src.replace("\n", "").replace(
            '"\n            "', " "))
        self.assertNotIn("Detected Platform:", src)


class FooterBarGateTests(unittest.TestCase):
    """Note #2: footer chrome chỉ render khi stdout là tty."""

    BINDINGS = [("↑↓", "di chuyển"), ("?", "help"), ("q", "thoát")]

    def test_pipe_stdout_renders_nothing(self):
        from ctf_downloader.ui.widgets import footer_bar

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(footer_bar(self.BINDINGS, width=120), "")

    def test_tty_stdout_renders_markup(self):
        from ctf_downloader.ui.widgets import footer_bar

        with contextlib.redirect_stdout(FakeTTY()):
            out = footer_bar(self.BINDINGS, width=120)
        self.assertIn("[hi_fg]q[/] thoát", out)

    def test_explicit_true_overrides_pipe(self):
        # Hợp đồng caller TUI (watch/menu): luôn tương tác → tty=True.
        from ctf_downloader.ui.widgets import footer_bar

        with contextlib.redirect_stdout(io.StringIO()):
            out = footer_bar(self.BINDINGS, width=120, tty=True)
        self.assertIn("thoát", out)

    def test_explicit_false_renders_nothing_on_tty(self):
        from ctf_downloader.ui.widgets import footer_bar

        with contextlib.redirect_stdout(FakeTTY()):
            self.assertEqual(footer_bar(self.BINDINGS, width=120, tty=False),
                             "")


class FramedCommandGateTests(unittest.TestCase):
    """`_run_framed` (status/workspaces/storage/…) qua widget gate:
    pipe → header+body machine-readable KHÔNG footer; tty giả lập → footer."""

    def _frame_output(self, buf_cls):
        from ctf_downloader import cli

        buf = buf_cls()
        with contextlib.redirect_stdout(buf):
            cli._run_framed(lambda a: print("BODY"),
                            SimpleNamespace(workspace="wsA"), "status")
        return buf.getvalue()

    @staticmethod
    def _strip_ansi(text):
        import re
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def test_pipe_has_body_without_footer_chrome(self):
        out = self._frame_output(io.StringIO)
        self.assertIn("BODY", out)
        self.assertNotIn("q thoát", out)
        self.assertNotIn("di chuyển", out)

    def test_fake_tty_keeps_full_chrome(self):
        # TTY giả lập → rich tô màu ANSI → strip trước khi assert nội dung.
        out = self._strip_ansi(self._frame_output(FakeTTY))
        self.assertIn("BODY", out)
        self.assertLess(out.index("CTF·TOOLKIT"), out.index("BODY"))
        self.assertGreater(out.index("q thoát"), out.index("BODY"))
        for frag in ("↑↓ di chuyển", "? help", "q thoát"):
            self.assertIn(frag, out)


if __name__ == "__main__":
    unittest.main()
