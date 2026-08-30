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
        # Siết: phải > 5.5s để phân biệt rõ gap 6.0 của registry với
        # fallback 5.0 (fallback sẽ cho waited ≈ 5.0 - overhead < 5.5).
        self.assertGreater(waited, 5.5)
        self.assertLess(waited, 7)

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


class TestLoadChallengesCacheBehavior(_SubmitCase):
    """Characterization cho _load_challenges (hành vi frozen của submitter cũ):
    challenges.json TỒN TẠI -> luôn dùng cache (kể cả mảng ``challenges`` rỗng),
    KHÔNG bao giờ rơi xuống fetch live; chỉ fetch live khi không đọc được file."""

    def _write_challenges(self, challs):
        with open(os.path.join(self.ws, "challenges.json"), "w", encoding="utf-8") as f:
            json.dump({
                "platform_url": "http://ctf.test",
                "ctf_info": {"url": "http://ctf.test"},
                "challenges": challs,
            }, f)

    def test_empty_challenges_array_uses_cache_no_live_fetch(self):
        self._write_challenges([])  # workspace có challenges.json nhưng mảng rỗng
        fs, platform = self.make_submitter()
        platform.fetch_challenges.assert_not_called()
        platform.authenticate.assert_not_called()
        self.assertEqual(fs.challenges_cache, {})

    def test_missing_challenges_json_falls_back_to_live_fetch(self):
        os.remove(os.path.join(self.ws, "challenges.json"))
        fs, platform = self.make_submitter()
        platform.authenticate.assert_called()
        platform.fetch_challenges.assert_called()


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

    # ---- Diagnostic sweep: auth fail / platform không hỗ trợ scoreboard ----

    def _make_service(self, platform):
        """RankService thẳng (không qua facade) với platform giả."""
        import ctf_downloader.services.rank_service as rs
        with patch.object(rs, "create_session", return_value=MagicMock()), \
             patch.object(rs.PlatformDetector, "detect_platform",
                          return_value=platform):
            return RankService(workspace_path=str(self.root),
                               url="https://r.test")

    def _diags(self, rd_mock):
        return [c.args[0] for c in rd_mock.call_args_list]

    def test_auth_fail_renders_warning_diagnostic_with_cookie_hint(self):
        plat = MagicMock()
        plat.authenticate.return_value = False
        plat.fetch_scoreboard.return_value = {
            "standings": [{"pos": 1, "name": "a", "score": 1}]}
        svc = self._make_service(plat)

        with patch("ctf_downloader.services.rank_service.render_diagnostic") as rd:
            data = svc.fetch_ranking()

        # Hành vi giữ nguyên: vẫn trả dữ liệu scoreboard công khai
        self.assertEqual(data["standings"][0]["name"], "a")
        warns = [d for d in self._diags(rd) if d.severity == "warning"]
        self.assertTrue(warns)
        self.assertTrue(any("cookie" in h.lower()
                            for d in warns for h in d.hints))

    def test_missing_scoreboard_support_raises_with_public_hint(self):
        plat = MagicMock()
        plat.authenticate.return_value = True
        del plat.fetch_scoreboard          # platform không có API scoreboard
        svc = self._make_service(plat)

        with patch("ctf_downloader.services.rank_service.render_diagnostic") as rd:
            with self.assertRaises(AttributeError):
                svc.fetch_ranking()        # giữ hành vi cũ: raise -> CLI exit 1

        errs = [d for d in self._diags(rd) if d.severity == "error"]
        self.assertTrue(errs)
        self.assertTrue(any("scoreboard công khai" in h
                            for d in errs for h in d.hints))

    def test_capability_scoreboard_false_hints_and_continues(self):
        plat = MagicMock()
        plat.authenticate.return_value = True
        plat.info.capabilities = {"scoreboard": False}   # contract: platform.info
        plat.fetch_scoreboard.return_value = {"standings": []}
        svc = self._make_service(plat)

        with patch("ctf_downloader.services.rank_service.render_diagnostic") as rd:
            data = svc.fetch_ranking()

        self.assertEqual(data, {"standings": []})   # flow đi tiếp như cũ
        self.assertTrue(any("scoreboard công khai" in h
                            for d in self._diags(rd) for h in d.hints))

    def test_fetch_scoreboard_error_renders_diagnostic_and_reraises(self):
        plat = MagicMock()
        plat.authenticate.return_value = True
        plat.fetch_scoreboard.side_effect = ConnectionError("reset by peer")
        svc = self._make_service(plat)

        with patch("ctf_downloader.services.rank_service.render_diagnostic") as rd:
            with self.assertRaises(ConnectionError):   # giữ hành vi cũ: raise
                svc.fetch_ranking()

        errs = [d for d in self._diags(rd) if d.severity == "error"]
        self.assertTrue(errs)
        self.assertIn("ConnectionError", errs[0].cause)
        self.assertTrue(any("kết nối mạng" in h for d in errs for h in d.hints))

    def test_normalized_http_500_is_error_not_empty_scoreboard_success(self):
        plat = MagicMock()
        plat.authenticate.return_value = True
        plat.fetch_scoreboard.return_value = {
            "standings": [], "_http_status": 500,
        }
        svc = self._make_service(plat)
        with patch("ctf_downloader.services.rank_service.render_diagnostic") as rd:
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                svc.fetch_ranking()
        self.assertTrue(any(d.severity == "error" for d in self._diags(rd)))

    def test_normalized_transport_error_is_reraised(self):
        plat = MagicMock()
        plat.authenticate.return_value = True
        plat.fetch_scoreboard.return_value = {
            "standings": [], "_error": "ConnectionError: reset",
        }
        svc = self._make_service(plat)
        with self.assertRaisesRegex(RuntimeError, "transport.*ConnectionError"):
            svc.fetch_ranking()

    def test_429_preserves_retry_after_in_error(self):
        plat = MagicMock()
        plat.authenticate.return_value = True
        plat.fetch_scoreboard.return_value = {
            "standings": [], "_http_status": 429, "_retry_after": "90",
        }
        svc = self._make_service(plat)
        with self.assertRaisesRegex(RuntimeError, "429.*Retry-After=90"):
            svc.fetch_ranking()

    def test_304_without_cached_snapshot_is_protocol_error(self):
        plat = MagicMock()
        plat.authenticate.return_value = True
        plat.fetch_scoreboard.return_value = {
            "standings": [], "_http_status": 304, "_not_modified": True,
        }
        svc = self._make_service(plat)
        with self.assertRaisesRegex(RuntimeError, "304.*snapshot"):
            svc.fetch_ranking()

class TestRankScoreboardPhosphor(unittest.TestCase):
    """Render PHOSPHOR của bảng xếp hạng (design-system spec §4):

    ``_render_scoreboard`` là hàm thuần từ dữ liệu scoreboard → không
    mạng/platform; smoke render chạy qua Console có load_theme (mock).
    """

    DATA = {
        "title": "RCTF Live", "my_team": "team_x", "my_user": None,
        "my_rank": 3, "my_score": 1500, "total_teams": 25,
        "standings": [
            {"pos": 1, "name": "top_guys", "score": 3000},
            {"pos": 2, "name": "runner_up", "score": 2800},
            {"pos": 3, "name": "team_x", "score": 1500},
            {"pos": 4, "name": "mid_table", "score": 900},
        ],
    }

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="arch5b_rank_ui_")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    # ---- helpers ------------------------------------------------------- #

    def _panel(self, data=None, top_n=10):
        return RankService._render_scoreboard(
            dict(self.DATA) if data is None else data, top_n=top_n)

    def _table(self, panel):
        from rich.table import Table
        body = panel.renderable
        tables = [r for r in body.renderables if isinstance(r, Table)]
        self.assertTrue(tables)
        return tables[0]

    def _cells(self, table, col_idx):
        return list(table.columns[col_idx]._cells)

    @staticmethod
    def _plain(cell):
        return cell.plain if hasattr(cell, "plain") else str(cell)

    def _render_smoke(self, panel):
        """Smoke: render qua console PHOSPHOR thật (không network)."""
        import io
        from rich.console import Console
        from ctf_downloader.ui.theme import load_theme

        out = io.StringIO()
        con = Console(file=out, width=110, theme=load_theme(None),
                      force_terminal=False, no_color=True)
        con.print(panel)
        return out.getvalue()

    # ---- panel chrome --------------------------------------------------- #

    def test_panel_rounded_accent_deep_with_faint_upper_heading(self):
        from rich import box as rich_box
        from rich.text import Text

        panel = self._panel()
        self.assertIs(panel.box, rich_box.ROUNDED)
        self.assertEqual(panel.border_style, "accent.deep")
        self.assertIsInstance(panel.title, Text)
        self.assertIn("BẢNG XẾP HẠNG · RCTF LIVE", panel.title.plain)
        self.assertEqual(panel.title.style, "fg.faint")

    def test_table_boxless_and_column_layout(self):
        table = self._table(self._panel())
        self.assertIsNone(table.box)          # bảng phẳng trong panel
        headers = [c.header for c in table.columns]
        self.assertEqual(headers, ["#", "TEAM / USER", "SCORE", "GAP"])
        self.assertEqual(table.columns[2].justify, "right")   # SCORE
        self.assertEqual(table.columns[3].justify, "right")   # GAP
        self.assertEqual(table.header_style, "fg.faint")

    # ---- hàng & glyph ---------------------------------------------------- #

    def test_top3_diamond_glyph_no_medal_emoji(self):
        table = self._table(self._panel())
        pos_cells = [self._plain(c) for c in self._cells(table, 0)]
        self.assertEqual(pos_cells, ["◆ 1", "◆ 2", "◆ 3", "4"])

        out = self._render_smoke(self._panel())
        for emoji in ("🥇", "🥈", "🥉", "🏆", "👉"):
            self.assertNotIn(emoji, out)
        self.assertIn("◆ 1", out)

    def test_my_row_chip_subtle_and_bold_name_others_plain(self):
        from ctf_downloader.ui.theme import ACCENT_DEEP, FG_BASE

        panel = self._panel()
        table = self._table(panel)
        rows = table.rows
        self.assertEqual(rows[2].style, f"on {ACCENT_DEEP}")     # team_x
        for i in (0, 1, 3):
            self.assertIsNone(rows[i].style)

        name_cells = self._cells(table, 1)
        self.assertEqual(name_cells[2].style, f"bold {FG_BASE}")
        self.assertEqual(name_cells[2].plain, "team_x")
        for i in (0, 1, 3):
            self.assertEqual(name_cells[i].style, "fg.base")

    # ---- footer tóm tắt --------------------------------------------------- #

    def test_footer_rank_and_gap_muted(self):
        from rich.text import Text

        panel = self._panel()
        foot = [r for r in panel.renderable.renderables
                if isinstance(r, Text)]
        self.assertEqual(len(foot), 1)
        self.assertEqual(foot[0].plain, "rank 3/25 · gap 1500 pts")
        self.assertEqual(foot[0].style, "fg.muted")

    def test_footer_omitted_without_rank_info(self):
        from rich.console import Group
        from rich.text import Text

        data = dict(self.DATA, my_rank=None, my_score=None,
                    my_team=None, total_teams=0)
        panel = self._panel(data)
        if isinstance(panel.renderable, Group):
            texts = [r for r in panel.renderable.renderables
                     if isinstance(r, Text)]
            self.assertEqual(texts, [])       # chỉ còn bảng trong group
        else:
            self.assertNotIsInstance(panel.renderable, Text)  # bảng trần

    def test_empty_standings_renders_placeholder_note(self):
        panel = self._panel({"title": "Empty CTF", "standings": []})
        from rich.table import Table
        from rich.text import Text

        note = panel.renderable               # body là Text, không Group
        self.assertIsInstance(note, Text)
        self.assertEqual(note.plain, "chưa có dữ liệu")
        self.assertEqual(note.style, "fg.faint")
        self.assertNotIsInstance(note, Table)

    # ---- smoke + wiring ---------------------------------------------------- #

    def test_smoke_render_resolves_all_tokens(self):
        out = self._render_smoke(self._panel(top_n=4))
        self.assertIn("BẢNG XẾP HẠNG · RCTF LIVE", out)
        self.assertIn("top_guys", out)
        self.assertIn("rank 3/25 · gap 1500 pts", out)

    def test_display_and_update_prints_scoreboard_panel_mocked(self):
        import ctf_downloader.services.rank_service as rs

        plat = MagicMock()
        plat.authenticate.return_value = True
        plat.fetch_scoreboard.return_value = dict(self.DATA)
        with patch.object(rs, "create_session", return_value=MagicMock()), \
             patch.object(rs.PlatformDetector, "detect_platform",
                          return_value=plat):
            svc = RankService(workspace_path=self._tmp,
                              url="https://r.test")
        svc.fetch_ranking = MagicMock(return_value=dict(self.DATA))

        with patch.object(rs, "_rank_console") as con:
            returned = svc.display_and_update(top_n=5, update_docs=False)

        self.assertEqual(returned["my_team"], "team_x")   # data đi nguyên
        self.assertEqual(con.print.call_count, 1)
        panel = con.print.call_args.args[0]
        self.assertEqual(panel.border_style, "accent.deep")



if __name__ == "__main__":
    unittest.main()
