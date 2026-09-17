"""Unit tests for Antigravity (agy / BQA) interactive recovery coordination."""
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ctf_downloader.bqa_recovery import (
    BqaRepairResult,
    RecoveryIncident,
    load_prompt_template,
    offer_bqa_recovery,
)
from ctf_downloader.incidents import (
    IncidentKind,
    RecoveryHint,
    RetryPlan,
    may_spawn_agy,
)
from ctf_downloader.services import instance_service as isvc
from ctf_downloader.ui.diagnostics import Diagnostic


class TestIncidentsAndPolicy(unittest.TestCase):
    def test_may_spawn_agy_guards(self):
        tty_mock = MagicMock()
        tty_mock.isatty.return_value = True

        # When PYTEST_CURRENT_TEST is set, must return False
        self.assertFalse(may_spawn_agy({"PYTEST_CURRENT_TEST": "test_foo"}, tty_mock))

        # When CI is set, must return False
        self.assertFalse(may_spawn_agy({"CI": "true"}, tty_mock))

        # When CTF_DISABLE_BQA is set, must return False
        self.assertFalse(may_spawn_agy({"CTF_DISABLE_BQA": "1"}, tty_mock))

        # When CTF_BQA_RETRY is set, must return False
        self.assertFalse(may_spawn_agy({"CTF_BQA_RETRY": "1"}, tty_mock))

        # When stdin is not a TTY, must return False
        non_tty_mock = MagicMock()
        non_tty_mock.isatty.return_value = False
        self.assertFalse(may_spawn_agy({}, non_tty_mock))

        # When clean env and TTY, must return True
        self.assertTrue(may_spawn_agy({}, tty_mock))

    def test_recovery_incident_from_hint(self):
        hint = RecoveryHint(
            kind=IncidentKind.UNSUPPORTED_PLATFORM_FEATURE,
            operation="instance.start",
            platform="asisctf",
            evidence={"challenge_id": "72", "adapter": "asisctf"},
            retry=RetryPlan(
                argv=("instance", "start", "--id", "72", "--cookie", "secret_cookie"),
                safe_to_retry=False,
            ),
            repair_eligible=True,
            error_code="CTF-INSTANCE-U01",
        )
        incident = RecoveryIncident.from_hint(hint, exit_code=1)
        self.assertEqual(incident.error_code, "CTF-INSTANCE-U01")
        self.assertEqual(incident.evidence.get("platform"), "asisctf")
        self.assertEqual(incident.evidence.get("operation"), "instance.start")
        # Ensure credentials in retry_plan are redacted in incident.argv
        self.assertNotIn("secret_cookie", incident.argv)
        self.assertIn("<redacted>", incident.argv)

    def test_load_general_prompt_templates(self):
        for code in ("CTF-INSTANCE-U01", "CTF-PLATFORM-D01", "CTF-AUTH-A01", "CTF-RUNTIME-R01"):
            prompt = load_prompt_template(code)
            self.assertIn(f"[{code}]", prompt)
            self.assertIn("Never print", prompt)


class TestDiagnosticBuildersRecovery(unittest.TestCase):
    def test_start_instance_unsupported_has_recovery(self):
        diag = isvc.diag_start_instance_fail(
            72,
            "LeakMeAk",
            "Quản lý instance không được hỗ trợ cho asisctf",
            platform_name="asisctf",
            workspace_path="/tmp/test_ws",
        )
        self.assertIsNotNone(diag.recovery)
        self.assertEqual(diag.recovery.kind, IncidentKind.UNSUPPORTED_PLATFORM_FEATURE)
        self.assertEqual(diag.recovery.platform, "asisctf")
        self.assertTrue(diag.recovery.repair_eligible)
        self.assertIn("--id", diag.recovery.retry.argv)
        self.assertIn("72", diag.recovery.retry.argv)

    def test_start_instance_normal_error_has_no_recovery(self):
        diag = isvc.diag_start_instance_fail(
            72,
            "LeakMeAk",
            "quota exceeded: slots full",
            platform_name="asisctf",
        )
        self.assertIsNone(diag.recovery)

    def test_stop_and_extend_builders(self):
        stop_diag = isvc.diag_stop_instance_fail(
            72, "LeakMeAk", "Quản lý instance không được hỗ trợ cho asisctf", platform_name="asisctf"
        )
        self.assertIsNotNone(stop_diag.recovery)
        self.assertEqual(stop_diag.recovery.operation, "instance.stop")

        extend_diag = isvc.diag_extend_instance_fail(
            72, "LeakMeAk", "not supported by platform", platform_name="asisctf"
        )
        self.assertIsNotNone(extend_diag.recovery)
        self.assertEqual(extend_diag.recovery.operation, "instance.extend")


class TestOfferBqaRecovery(unittest.TestCase):
    def setUp(self):
        self.hint = RecoveryHint(
            kind=IncidentKind.UNSUPPORTED_PLATFORM_FEATURE,
            operation="instance.start",
            platform="asisctf",
            evidence={"challenge_id": "72"},
            retry=RetryPlan(argv=("instance", "start", "--id", "72"), safe_to_retry=True),
            repair_eligible=True,
            error_code="CTF-INSTANCE-U01",
        )
        self.diag = Diagnostic("error", "Failed to start", recovery=self.hint)

    def test_offer_skipped_if_no_recovery(self):
        diag_no_rec = Diagnostic("error", "Some error")
        self.assertFalse(offer_bqa_recovery(diag_no_rec))

    def test_offer_skipped_if_policy_fails(self):
        fake_stdin = MagicMock()
        fake_stdin.isatty.return_value = False
        self.assertFalse(offer_bqa_recovery(self.diag, input_stream=fake_stdin))

    def test_offer_skipped_on_user_enter(self):
        fake_stdin = io.StringIO("\n")
        fake_stdin.isatty = lambda: True
        fake_stdout = io.StringIO()

        with patch.dict(os.environ, {}, clear=False):
            # Temporarily clear PYTEST_CURRENT_TEST in env dict for this call
            env_clean = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
            with patch("ctf_downloader.incidents.os.environ", env_clean):
                handled = offer_bqa_recovery(
                    self.diag,
                    input_stream=fake_stdin,
                    output_stream=fake_stdout,
                )
        self.assertFalse(handled)
        output = fake_stdout.getvalue()
        self.assertIn("Antigravity can inspect this local source checkout", output)

    def test_offer_diagnose_and_repair_mode_a(self):
        fake_stdin = io.StringIO("a\ny\n")
        fake_stdin.isatty = lambda: True
        fake_stdout = io.StringIO()

        runner_mock = MagicMock()
        runner_mock.repair.return_value = BqaRepairResult(
            conversation_id="conv-1234",
            returncode=0,
            changed_test_paths=("tests/test_asisctf.py",),
            reason="BQA repair completed",
        )

        test_root = Path("/tmp/test_source_root")
        env_clean = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
        with patch("ctf_downloader.incidents.os.environ", env_clean):
            with patch("ctf_downloader.bqa_recovery.verify_bqa_changes", return_value=True):
                with patch("ctf_downloader.bqa_recovery.retry_command", return_value=0) as retry_mock:
                    handled = offer_bqa_recovery(
                        self.diag,
                        source_root=test_root,
                        input_stream=fake_stdin,
                        output_stream=fake_stdout,
                        runner=runner_mock,
                    )

        self.assertTrue(handled)
        runner_mock.repair.assert_called_once()
        retry_mock.assert_called_once_with(self.hint.retry.argv, test_root.resolve())
        self.assertIn("All verification gates passed.", fake_stdout.getvalue())

    def test_offer_interactive_mode_i(self):
        fake_stdin = io.StringIO("i\nn\n")
        fake_stdin.isatty = lambda: True
        fake_stdout = io.StringIO()

        env_clean = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
        with patch("ctf_downloader.incidents.os.environ", env_clean):
            with patch("ctf_downloader.bqa_recovery.resolve_agy_binary", return_value="/usr/bin/agy"):
                with patch("ctf_downloader.bqa_recovery.subprocess.run") as sub_run:
                    handled = offer_bqa_recovery(
                        self.diag,
                        input_stream=fake_stdin,
                        output_stream=fake_stdout,
                    )

        self.assertTrue(handled)
        sub_run.assert_called_once()
        self.assertIn("Handing terminal to Antigravity", fake_stdout.getvalue())


class TestInstanceServiceIntegration(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="inst_diag_rec_")
        root = Path(self._tmp) / "ws"
        root.mkdir(parents=True)
        (root / "challenges.json").write_text(json.dumps({
            "ctf_info": {"url": "https://asis.example.com", "platform": "asisctf"},
        }), encoding="utf-8")
        meta = root / "Misc" / "LeakMeAk"
        meta.mkdir(parents=True)
        (meta / "metadata.json").write_text(json.dumps({
            "id": 72, "name": "LeakMeAk", "raw": {"type": "dynamic"}}), encoding="utf-8")
        self.svc = isvc.InstanceService(str(root))
        self.svc.platform = MagicMock()
        self.svc.platform.ctf_info = MagicMock()
        self.svc.platform.ctf_info.platform_type = "asisctf"
        self.svc.platform.name = "asisctf"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_start_instance_sets_last_diagnostic_with_recovery(self):
        self.svc.platform.start_instance.return_value = (
            False,
            {"message": "Quản lý instance không được hỗ trợ cho asisctf"},
        )
        with patch.object(isvc, "render_diagnostic") as rmock:
            ok, info = self.svc.start_instance(72)

        self.assertFalse(ok)
        self.assertIsNotNone(self.svc.last_diagnostic)
        self.assertEqual(self.svc.last_diagnostic.recovery.platform, "asisctf")
        self.assertEqual(self.svc.last_diagnostic.recovery.kind, IncidentKind.UNSUPPORTED_PLATFORM_FEATURE)


class TestInteractiveMenuContainerRecovery(unittest.TestCase):
    def test_container_action_calls_offer_bqa_recovery_on_failure(self):
        from ctf_downloader.interactive_menu import CTFInteractiveConsole

        menu = CTFInteractiveConsole(workspace_path="/tmp/fake_ws")
        mock_mgr = MagicMock()
        mock_diag = Diagnostic(
            "error",
            "Cannot start instance",
            recovery=RecoveryHint(
                kind=IncidentKind.UNSUPPORTED_PLATFORM_FEATURE,
                operation="instance.start",
                platform="asisctf",
            ),
        )
        mock_mgr.last_diagnostic = mock_diag
        mock_mgr.start_instance.return_value = (False, {"message": "unsupported"})

        with patch("ctf_downloader.interactive_menu.InstanceManager", return_value=mock_mgr):
            with patch("ctf_downloader.interactive_menu._prompt", return_value="1"):
                with patch("ctf_downloader.bqa_recovery.offer_bqa_recovery", return_value=True) as offer_mock:
                    with patch("ctf_downloader.interactive_menu._pause") as pause_mock:
                        menu._run_container_action_for_id("72", "LeakMeAk")

        mock_mgr.start_instance.assert_called_once_with("72")
        offer_mock.assert_called_once_with(mock_diag)
        pause_mock.assert_not_called()

    def test_menu_container_manager_direct_id_when_no_containers(self):
        from ctf_downloader.interactive_menu import CTFInteractiveConsole

        menu = CTFInteractiveConsole(workspace_path="/tmp/fake_ws")
        mock_mgr = MagicMock()
        mock_mgr.list_containers.return_value = []
        mock_mgr.find_challenge.return_value = {"id": 72, "name": "LeakMeAk"}

        with patch("ctf_downloader.interactive_menu.InstanceManager", return_value=mock_mgr):
            with patch("ctf_downloader.interactive_menu._prompt", return_value="72"):
                with patch.object(menu, "_run_container_action_for_id") as run_mock:
                    menu._menu_container_manager()

        run_mock.assert_called_once_with("72", challenge_name="LeakMeAk")

    def test_menu_container_manager_cancel_when_no_containers(self):
        from ctf_downloader.interactive_menu import CTFInteractiveConsole

        menu = CTFInteractiveConsole(workspace_path="/tmp/fake_ws")
        mock_mgr = MagicMock()
        mock_mgr.list_containers.return_value = []

        with patch("ctf_downloader.interactive_menu.InstanceManager", return_value=mock_mgr):
            with patch("ctf_downloader.interactive_menu._prompt", return_value="0"):
                with patch.object(menu, "_run_container_action_for_id") as run_mock:
                    menu._menu_container_manager()

        run_mock.assert_not_called()
