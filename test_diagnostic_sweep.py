"""Diagnostic sweep submit/instance — mỗi lỗi nghiệp vụ chính phải được
render dưới dạng ``Diagnostic`` (ui/diagnostics.py) với hints hành động được,
thay vì Logger.error/warning thô.

Chạy: python3 -m unittest test_diagnostic_sweep.py -v
"""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ctf_downloader.services import submit_service as ss
from ctf_downloader.services import instance_service as isvc
from ctf_downloader.submitter import FlagSubmitter  # facade mỏng
from ctf_downloader.instance_manager import InstanceManager  # facade mỏng
from ctf_downloader.ui.diagnostics import Diagnostic


def _make_mock_platform():
    p = MagicMock()
    p.ctf_info.platform_type = "ctfd"
    p.authenticate.return_value = True
    p.fetch_challenges.return_value = []
    p.fetch_rules.return_value = None
    p.submit_flag.return_value = (True, "ok")
    p.last_verdict = "correct"
    return p


def _assert_diag_with_hints(case, render_mock, min_hints=1):
    """Render mock phải nhận đúng 1 Diagnostic và hints không rỗng."""
    case.assertEqual(render_mock.call_count, 1)
    diag = render_mock.call_args[0][0]
    case.assertIsInstance(diag, Diagnostic)
    case.assertTrue(diag.hints, f"Diagnostic '{diag.message}' phải có hints")
    case.assertGreaterEqual(len(diag.hints), min_hints)
    return diag


class TempWorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="diag_sweep_ws_")
        with open(os.path.join(self.ws, "challenges.json"), "w", encoding="utf-8") as f:
            json.dump({
                "platform_url": "http://ctf.test",
                "ctf_info": {"url": "http://ctf.test"},
                "challenges": [{"id": 1, "name": "Chall One", "category": "Web"}],
            }, f)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def _make_submitter(self, **kwargs):
        platform = _make_mock_platform()
        with patch.object(ss.PlatformDetector, "detect_platform", return_value=platform):
            fs = FlagSubmitter(url="http://ctf.test", workspace_dir=self.ws, **kwargs)
        return fs, platform


# ----------------------------------------------------------------------
# Lỗi 1: submit bị khoá vì thiếu flag format
# ----------------------------------------------------------------------
class TestNoFormatDiagnostic(TempWorkspaceCase):
    def test_no_format_renders_diagnostic_with_actionable_hints(self):
        fs, platform = self._make_submitter()
        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("builtins.input", side_effect=AssertionError("must not prompt")), \
             patch.object(ss, "render_diagnostic") as rmock:
            succ, msg = fs.submit(1, "ANYTHING{abc}")

        self.assertFalse(succ)
        self.assertIn("flag format", msg)
        platform.submit_flag.assert_not_called()
        diag = _assert_diag_with_hints(self, rmock)
        joined = " ".join(diag.hints)
        self.assertIn("--flag-format", joined)  # hint phải chỉ ra lệnh cụ thể

    def test_builder_direct(self):
        diag = ss.diag_no_format()
        self.assertTrue(diag.hints)


# ----------------------------------------------------------------------
# Lỗi 2: blacklist chặn flag đã sai trước đó
# ----------------------------------------------------------------------
class TestBlacklistDiagnostic(TempWorkspaceCase):
    def test_blacklist_renders_diagnostic_mentioning_force(self):
        with open(os.path.join(self.ws, "submit_history.json"), "w", encoding="utf-8") as f:
            json.dump({"entries": [{
                "flag": "TEST{bad}", "challenge_id": 99,
                "result": "incorrect", "timestamp": "2026-08-24T00:00:00Z",
            }]}, f)
        fs, platform = self._make_submitter(flag_format="^TEST\\{.+\\}$")

        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep"), \
             patch.object(ss, "render_diagnostic") as rmock:
            succ, msg = fs.submit(1, "TEST{bad}")

        self.assertFalse(succ)
        self.assertIn("blacklist", msg)
        platform.submit_flag.assert_not_called()
        diag = _assert_diag_with_hints(self, rmock)
        joined = " ".join(diag.hints)
        self.assertIn("--force", joined)  # hint phải chỉ đường vượt qua

    def test_builder_direct(self):
        diag = ss.diag_blacklisted(prev_cid=99)
        self.assertTrue(any("--force" in h for h in diag.hints))


# ----------------------------------------------------------------------
# Lỗi 3: instance create fail
# ----------------------------------------------------------------------
class TestInstanceStartFailDiagnostic(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="diag_sweep_inst_")
        root = Path(self._tmp) / "ws"
        root.mkdir(parents=True)
        (root / "challenges.json").write_text(json.dumps({
            "ctf_info": {"url": "https://gz.example.com", "platform": "gzctf"},
        }), encoding="utf-8")
        meta = root / "Web" / "dyn"
        meta.mkdir(parents=True)
        (meta / "metadata.json").write_text(json.dumps({
            "id": 10, "name": "Dyn A", "raw": {"type": "dynamic_docker"}}), encoding="utf-8")
        self.mgr = InstanceManager(str(root))
        self.mgr.platform = MagicMock()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_start_fail_renders_diagnostic_with_hints(self):
        self.mgr.platform.start_instance.return_value = (False, {"message": "quota exceeded"})
        with patch.object(isvc, "render_diagnostic") as rmock:
            ok, info = self.mgr.start_instance(10)

        self.assertFalse(ok)
        self.assertEqual(info.get("message"), "quota exceeded")
        diag = _assert_diag_with_hints(self, rmock)
        joined = " ".join(diag.hints)
        self.assertIn("ctf doctor", joined)  # hint phải hành động được
        self.assertIn("Dyn A", diag.message)

    def test_builder_direct(self):
        diag = isvc.diag_start_instance_fail(10, "Dyn A", "quota exceeded")
        self.assertEqual(diag.cause, "quota exceeded")
        self.assertTrue(diag.hints)


# ----------------------------------------------------------------------
# Lỗi phụ: platform detect fail (submit + instance) — builder hints
# ----------------------------------------------------------------------
class TestDetectFailureBuilders(unittest.TestCase):
    def test_submit_detect_builder_has_doctor_hint(self):
        diag = ss.diag_detect_failure(ValueError("bad url"))
        self.assertEqual(diag.severity, "error")
        self.assertTrue(any("ctf doctor" in h for h in diag.hints))

    def test_instance_detect_builder_has_doctor_hint(self):
        diag = isvc.diag_detect_failure(ValueError("no url"))
        self.assertEqual(diag.severity, "error")
        self.assertTrue(any("ctf doctor" in h for h in diag.hints))


if __name__ == "__main__":
    unittest.main()
