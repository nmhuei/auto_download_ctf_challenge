"""
Phase 5b — Task 9: submit/instance/rank services + facades.

Kiểm chứng:
  - FlagSubmitter/InstanceManager/RankingManager là facade mỏng (subclass của
    SubmitService/InstanceService/RankService).
  - Throttle đọc từ platform registry (spec.throttle), THROTTLE_BY_PLATFORM
    hardcode đã bị xoá khỏi submitter.
  - InstanceService.sync_containers() + interactive_pick().

Chạy: python3 -m pytest test_arch_phase5b.py -q
"""
import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from ctf_downloader.services.submit_service import SubmitService, NO_FORMAT_MESSAGE
from ctf_downloader.services.instance_service import InstanceService
from ctf_downloader.services.rank_service import RankService
from ctf_downloader.submitter import FlagSubmitter
from ctf_downloader.instance_manager import InstanceManager
from ctf_downloader.ranking import RankingManager


def make_resp(status_code=200, json_data=None, text="", headers=None):
    r = MagicMock()
    r.status_code = status_code
    if json_data is not None:
        r.json.return_value = json_data
    else:
        r.json.side_effect = ValueError("no json")
    r.text = text if text != "" else (json.dumps(json_data) if json_data is not None else "")
    r.headers = headers or {}
    return r


class TestFacadesAreThin(unittest.TestCase):
    def test_submitter_facade(self):
        self.assertTrue(issubclass(FlagSubmitter, SubmitService))
        # Hằng message công khai giữ nguyên qua facade
        from ctf_downloader.submitter import NO_FORMAT_MESSAGE as FACADE_NO_FORMAT
        self.assertEqual(FACADE_NO_FORMAT, NO_FORMAT_MESSAGE)

    def test_instance_facade(self):
        self.assertTrue(issubclass(InstanceManager, InstanceService))

    def test_rank_facade(self):
        self.assertTrue(issubclass(RankingManager, RankService))

    def test_throttle_hardcode_removed_from_submitter_module(self):
        import ctf_downloader.submitter as sub_mod
        import ctf_downloader.services.submit_service as svc_mod
        self.assertFalse(hasattr(sub_mod, "THROTTLE_BY_PLATFORM"))
        self.assertFalse(hasattr(svc_mod, "THROTTLE_BY_PLATFORM"))


class _SubmitCase(unittest.TestCase):
    """Workspace tối thiểu + FlagSubmitter mock hoàn toàn (như test_sp1_submit)."""

    def setUp(self):
        self.ws = tempfile.mkdtemp(prefix="sp5b_ws_")
        with open(os.path.join(self.ws, "challenges.json"), "w", encoding="utf-8") as f:
            json.dump({
                "platform_url": "http://ctf.test",
                "ctf_info": {"url": "http://ctf.test"},
                "challenges": [{"id": 1, "name": "Chall One", "category": "Web"}],
            }, f)

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def make_submitter(self, platform=None, **kwargs):
        platform = platform or MagicMock()
        platform.authenticate.return_value = True
        platform.fetch_challenges.return_value = []
        platform.fetch_rules.return_value = None
        platform.submit_flag.return_value = (True, "ok")
        platform.last_verdict = "correct"
        with patch("ctf_downloader.submitter.create_session", return_value=MagicMock()), \
             patch("ctf_downloader.submitter.PlatformDetector.detect_platform", return_value=platform):
            fs = FlagSubmitter(url="http://ctf.test", workspace_dir=self.ws, **kwargs)
        return fs, platform


class TestThrottleRegistryDriven(_SubmitCase):
    def _two_submit_wait(self, ptype):
        fs, platform = self.make_submitter(flag_format="^TEST\\{.+\\}$")
        platform.ctf_info.platform_type = ptype

        with patch.object(FlagSubmitter, "_stdout_isatty", return_value=False), \
             patch("ctf_downloader.submitter.time.sleep") as mock_sleep:
            fs.submit(1, "TEST{aaa}")
            fs.submit(1, "TEST{bbb}")

        self.assertEqual(mock_sleep.call_count, 1)
        return mock_sleep.call_args[0][0]

    def test_ctfd_uses_registry_gap_6(self):
        waited = self._two_submit_wait("ctfd")
        self.assertGreater(waited, 0)
        self.assertLess(waited, 7)  # gap 6s, không phải fallback 5s cũ

    def test_gzctf_uses_registry_gap_2_not_old_default(self):
        waited = self._two_submit_wait("gzctf")
        self.assertGreater(waited, 0)
        self.assertLess(waited, 3)  # gap 2s — chứng tỏ đọc registry chứ không hardcode

    def test_unknown_platform_falls_back_to_default(self):
        fs, platform = self.make_submitter()
        platform.ctf_info.platform_type = "zz_unknown_platform"
        min_gap, ptype = fs._resolve_min_gap()
        self.assertEqual(min_gap, 5.0)
        self.assertEqual(ptype, "zz_unknown_platform")


class TestInstanceSyncAndPick(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch5b_inst_")
        root = pathlib.Path(self._tmp) / "ws"
        c1 = root / "Web" / "dyn_a"
        c2 = root / "Web" / "dyn_b"
        for d in (c1, c2):
            d.mkdir(parents=True, exist_ok=True)
        (root / "challenges.json").write_text(json.dumps({
            "ctf_info": {"url": "https://gz.example.com", "platform": "gzctf"},
        }), encoding="utf-8")
        (c1 / "metadata.json").write_text(json.dumps({
            "id": 10, "name": "Dyn A", "raw": {"type": "dynamic_docker"}}), encoding="utf-8")
        (c2 / "metadata.json").write_text(json.dumps({
            "id": 11, "name": "Dyn B", "raw": {"type": "dynamic_docker"}}), encoding="utf-8")
        self.mgr = InstanceManager(str(root))
        self.mgr.platform = MagicMock()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_sync_containers_counts_active(self):
        self.mgr.platform.get_instance_status.side_effect = lambda cid: (
            {"status": "running", "entry": "1.2.3.4:9999"} if str(cid) == "10"
            else {"status": "stopped"}
        )
        active = self.mgr.sync_containers()
        self.assertEqual(active, 1)
        self.assertEqual(self.mgr.platform.get_instance_status.call_count, 2)

    def test_sync_containers_empty_workspace(self):
        self.mgr.list_containers = MagicMock(return_value=[])
        self.assertEqual(self.mgr.sync_containers(), 0)

    def test_interactive_pick_start_flow(self):
        container = {"id": 10, "name": "Dyn A", "category": "Web"}
        self.mgr.list_containers = MagicMock(return_value=[container])
        self.mgr.start_instance = MagicMock()

        with patch("builtins.input", side_effect=["1", "1"]):  # chọn chall 1 -> action start
            self.mgr.interactive_pick()

        self.mgr.start_instance.assert_called_once_with(10)

    def test_interactive_pick_invalid_choice_returns(self):
        self.mgr.list_containers = MagicMock(return_value=[{"id": 10, "name": "Dyn A", "category": "Web"}])
        self.mgr.start_instance = MagicMock()
        self.mgr.stop_instance = MagicMock()
        self.mgr.extend_instance = MagicMock()
        self.mgr.get_status = MagicMock()

        with patch("builtins.input", side_effect=["99", "1"]):  # choice ngoài phạm vi
            self.mgr.interactive_pick()

        self.mgr.start_instance.assert_not_called()
        self.mgr.stop_instance.assert_not_called()


class TestRankServicePath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch5b_rank_")
        root = pathlib.Path(self._tmp) / "ws"
        root.mkdir(parents=True)
        (root / "SUMMARY.md").write_text("# Summary\n- **Total Files Downloaded**: 7\n", encoding="utf-8")
        self.root = root

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _make_ranking_manager(self):
        with patch("ctf_downloader.ranking.create_session", return_value=MagicMock()), \
             patch("ctf_downloader.ranking.PlatformDetector.detect_platform", return_value=MagicMock()):
            return RankingManager(workspace_path=str(self.root), url="https://r.test")

    def test_display_and_update_via_facade_writes_docs(self):
        data = {
            "title": "RCTF Live", "my_team": "team_x", "my_user": None,
            "my_rank": 3, "my_score": 1500, "total_teams": 25,
            "standings": [{"pos": 1, "name": "top_guys", "score": 3000}],
        }
        mgr = self._make_ranking_manager()
        mgr.fetch_ranking = MagicMock(return_value=data)

        returned = mgr.display_and_update(top_n=5, update_docs=True)

        self.assertEqual(returned, data)
        self.assertTrue((self.root / "RANKING.md").exists())
        summary = (self.root / "SUMMARY.md").read_text(encoding="utf-8")
        self.assertEqual(summary.count("- **Live Rank**:"), 1)

    def test_display_and_update_no_standings_no_docs(self):
        data = {"title": "Empty CTF", "standings": []}
        mgr = self._make_ranking_manager()
        mgr.fetch_ranking = MagicMock(return_value=data)

        with patch("ctf_downloader.services.rank_service.Logger.warning") as warn:
            returned = mgr.display_and_update(update_docs=True)

        self.assertEqual(returned, data)
        warn.assert_called_once()
        self.assertFalse((self.root / "RANKING.md").exists())


if __name__ == "__main__":
    unittest.main()
