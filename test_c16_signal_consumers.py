"""Review follow-up commit 3e0fbcc (hunter-c16) — tiêu thụ tín hiệu skip.

Commit 3e0fbcc đã land TÍN HIỆU skip ở tầng storage (locked_write_text trả
False / update_status trả StatusWriteResult với .noop/.persisted) nhưng nhiều
caller chưa tiêu thụ tín hiệu đó. 4 finding review xác nhận:

  F1 [MED] generator/workspace_builder.py — create_challenge_workspace bỏ
      qua return False của locked_write_text: metadata.json bị skip IM LẶNG
      khi thư mục challenge bị xoá giữa lúc build. Fix: warning rõ tên +
      đường dẫn — không được nuốt im lặng.
  F2 [MED] status_service.set_note/update_tags + cli_commands.
      _handle_hoard_remove — in ✔ success vô điều kiện sau update_status.
      Phân loại qua StatusWriteResult: ``noop`` → thông điệp trung tính
      "không có gì thay đổi"; ``persisted=False`` mà không noop (ghi SKIP do
      thư mục/metadata biến mất) → cảnh báo lỗi, KHÔNG success.
  F3 [LOW] pull_service.sync_solve_attribution — ``updated += 1`` chưa xem
      ``.persisted``: chỉ đếm khi ghi THẬT SỰ persist (noop hoặc skip
      không đếm).
  F4 [LOW] storage/fileio.py — cửa sổ TOCTOU giữa re-check sau flock và
      open lockfile: process đang XẾP HÀNG nhận FileNotFoundError thay vì
      tín hiệu skip sạch như holder. Fix: bọc open(lockfile) try/except
      FileNotFoundError → trả cùng tín hiệu skip (False/None/
      yield-không-khóa).

Quy ước hunter/review: test FAIL có chủ ý = finding đang tái hiện; PASS =
documentation hành vi sau fix.
Chạy: python3 -m pytest test_c16_signal_consumers.py -q
"""
import argparse
import json
import os
import pathlib
import shutil
import tempfile
import types
import unittest
import unittest.mock as mock
from pathlib import Path

sys_path_added = False
if not sys_path_added:
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    sys_path_added = True

from ctf_downloader.generator import workspace_builder as wb
from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
from ctf_downloader.services.status_service import StatusService
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.storage.fileio import (
    atomic_write_text,
    locked_path,
    locked_update_json,
    locked_write_text,
)
from ctf_downloader.storage import workspace_repo as wr_mod
from ctf_downloader.storage.workspace_repo import (
    StatusWriteResult,
    WorkspaceRepo,
    normalize_status,
)
from ctf_downloader.utils.logger import Logger


# ----------------------------------------------------------------------
# Fixture workspace chung
# ----------------------------------------------------------------------

def _make_ws(root: Path) -> None:
    """2 challenge Web trùng tiền tố tên (ssti_*) để test resolve."""
    for slug in ("ssti_playground", "ssti_advanced"):
        (root / "Web" / slug).mkdir(parents=True, exist_ok=True)
    chals = [
        {"id": 1, "name": "SSTI Playground", "category": "Web", "points": 100},
        {"id": 2, "name": "SSTI Advanced", "category": "Web", "points": 200},
    ]
    (root / "challenges.json").write_text(json.dumps({
        "ctf_info": {"title": "SigCTF", "url": "https://sig.example.com",
                     "platform": "gzctf"},
        "challenges": chals,
    }), encoding="utf-8")
    for c in chals:
        slug = c["name"].lower().replace(" ", "_")
        (root / "Web" / slug / "metadata.json").write_text(
            json.dumps(c), encoding="utf-8")


# ----------------------------------------------------------------------
# F4 [LOW] — TOCTOU giữa re-check is_dir và open(lockfile)
# ----------------------------------------------------------------------

class _DirVanishRig:
    """Xoá thư mục cha NGAY SAU lần gọi ``Path.is_dir`` đầu tiên TRẢ TRUE
    trên chính thư mục đó — nén cửa sổ TOCTOU giữa re-check ``is_dir`` và
    ``open(lockfile)`` về 0 để tái hiện deterministic. Không có rig này,
    race chỉ xảy ra với process xếp hàng thật."""

    def __init__(self, parent):
        self.target = Path(parent)
        self._orig = pathlib.Path.is_dir
        self.fired = False

    def __enter__(self):
        orig, target = self._orig, self.target

        def rigged(path_obj):
            result = orig(path_obj)
            if not self.fired and path_obj == target and result:
                self.fired = True
                shutil.rmtree(target)
            return result

        pathlib.Path.is_dir = rigged
        return self

    def __exit__(self, *exc):
        pathlib.Path.is_dir = self._orig
        return False


class TestF4FileioToctouOpenLockfile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir(parents=True)

    def _make_target(self) -> Path:
        d = self.root / "web" / "alpha"
        d.mkdir(parents=True)
        mp = d / "metadata.json"
        atomic_write_text(mp, '{"id": 1}')
        return mp

    def test_locked_write_text_tra_false_khi_dir_mat_truoc_khi_mo_lockfile(self):
        mp = self._make_target()
        with _DirVanishRig(mp.parent):
            try:
                wrote = locked_write_text(mp, "v2")
            except FileNotFoundError as e:
                self.fail(f"phải trả tín hiệu skip chứ không raise: {e}")
        self.assertFalse(wrote, "dir biến mất trước khi mở lockfile -> skip (False)")
        self.assertFalse(mp.parent.exists(),
                         "không được hồi sinh thư mục đã xoá")

    def test_locked_update_json_tra_none_khi_dir_mat_truoc_khi_mo_lockfile(self):
        mp = self._make_target()
        with _DirVanishRig(mp.parent):
            try:
                out = locked_update_json(mp, lambda d: {**(d or {}), "x": 1})
            except FileNotFoundError as e:
                self.fail(f"phải trả tín hiệu skip chứ không raise: {e}")
        self.assertIsNone(out, "dir biến mất trước khi mở lockfile -> None (skip)")
        self.assertFalse(mp.parent.exists())

    def test_locked_path_yield_khong_khoa_khi_dir_mat_truoc_khi_mo_lockfile(self):
        """locked_path không có giá trị trả về để báo skip — đối xứng với
        nhánh parent-gone hiện hữu: YIELD KHÔNG KHÓA, ghi của caller fail
        LOÁ. Điều kiện cứng: body phải chạy (không nổ ngay ở open-lockfile)."""
        mp = self._make_target()
        state = {"body_ran": False, "raised": False}
        with _DirVanishRig(mp.parent):
            try:
                with locked_path(mp) as p:
                    state["body_ran"] = True
                    atomic_write_text(p, "v2")   # ghi vào dir đã mất: fail LOÁ
            except FileNotFoundError:
                state["raised"] = True
        self.assertTrue(state["body_ran"],
                        "locked_path phải yield (như nhánh parent-gone), "
                        "không nổ im lặng ở open(lockfile)")
        self.assertTrue(state["raised"], "ghi vào dir đã mất vẫn phải fail loud")
        self.assertFalse(mp.parent.exists())


# ----------------------------------------------------------------------
# F1 [MED] — builder không được nuốt tín hiệu skip của locked_write_text
# ----------------------------------------------------------------------

class TestF1BuilderMetadataSkip(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    @staticmethod
    def _chall():
        return types.SimpleNamespace(
            id=42, name="Skip Meta", category="Web", points=100,
            author="organizer", tags=[], hints=[],
            connection_info="nc 1.2.3.4 1337",
            solved_by_me=False, solves_count=3, submit_endpoint=None,
            instance_info=None, raw_data={}, description="<p>desc</p>")

    def _build(self) -> str:
        return WorkspaceBuilder.create_challenge_workspace(
            base_output_dir=str(self.base),
            challenge=self._chall(),
            extracted_links=[], connections=[],
            download_results=[], create_solve_template=False)

    def test_metadata_skip_phai_warning_ro_khong_nuot_lang(self):
        """Thư mục challenge bị xoá NGAY TRƯỚC lần ghi metadata.json
        (rig xoá rồi mới delegate sang locked_write_text thật) -> builder
        phải WARNING rõ (tên challenge + file), không crash, không im lặng."""
        real_lwt = wb.locked_write_text

        def rmtree_then_real(path, text):
            shutil.rmtree(Path(str(path)).parent)
            return real_lwt(path, text)

        with mock.patch.object(wb, "locked_write_text",
                               side_effect=rmtree_then_real), \
             mock.patch.object(wb.Logger, "warning") as m_warn:
            challenge_dir = self._build()

        self.assertFalse((Path(challenge_dir) / "metadata.json").exists(),
                         "ghi đã bị skip — metadata không được sinh zombie")
        self.assertTrue(m_warn.called,
                        "caller bắt buộc phải tiêu thụ return False — "
                        "không được nuốt im lặng")
        joined = " ".join(str(c.args[0]) for c in m_warn.call_args_list)
        self.assertIn("Skip Meta", joined, "warning phải nêu tên challenge")
        self.assertIn("metadata.json", joined, "warning phải nêu file bị skip")
        self.assertIn(str(challenge_dir), joined,
                      "warning phải nêu đường dẫn workspace bị ảnh hưởng")

    def test_build_binh_thuong_van_ghi_metadata_khong_warning(self):
        # Positive control: đường vui không bị phá — metadata ghi đủ, 0 warning.
        with mock.patch.object(wb.Logger, "warning") as m_warn:
            challenge_dir = self._build()
        self.assertTrue((Path(challenge_dir) / "metadata.json").exists())
        self.assertFalse(m_warn.called)


# ----------------------------------------------------------------------
# F2 [MED] — note/tag/gỡ-flag: phân biệt noop vs skip qua StatusWriteResult
# ----------------------------------------------------------------------

def _rig_update_status_vanish():
    """Patch WorkspaceRepo.update_status: xoá thư mục challenge TRƯỚC khi
    delegate sang bản thật — mô phỏng 'thư mục biến mất giữa lúc ghi' mà
    resolve_challenge vẫn đọc được metadata lúc tìm kiếm."""
    real = WorkspaceRepo.update_status

    def rigged(repo_self, mp, mut):
        shutil.rmtree(Path(str(mp)).parent)
        return real(repo_self, mp, mut)

    return mock.patch.object(WorkspaceRepo, "update_status", rigged)


class TestF2NoteTagSignal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c16sig_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.root = Path(self._tmp) / "ws"
        _make_ws(self.root)
        self.repo = WorkspaceRepo(self.root)

    def _meta(self, slug: str) -> Path:
        return self.root / "Web" / slug / "metadata.json"

    # ---- SKIP: ghi bị bỏ qua (thư mục biến mất) ---------------------- #
    def test_set_note_skip_khong_duoc_success_phai_canh_bao_loi(self):
        with _rig_update_status_vanish(), \
             mock.patch.object(Logger, "success") as m_ok, \
             mock.patch.object(Logger, "error") as m_err:
            ok = StatusService.set_note(self.repo, "SSTI Playground", text="x")
        self.assertFalse(ok, "ghi bị skip phải báo thất bại (CLI exit 1)")
        m_ok.assert_not_called(), "KHÔNG được in ✔ success khi ghi bị skip"
        self.assertTrue(m_err.called, "phải cảnh báo lỗi rõ ràng")
        msg = " ".join(str(c.args[0]) for c in m_err.call_args_list)
        self.assertIn("SSTI Playground", msg)

    def test_remove_note_skip_khong_duoc_success_phai_canh_bao_loi(self):
        with _rig_update_status_vanish(), \
             mock.patch.object(Logger, "success") as m_ok, \
             mock.patch.object(Logger, "error") as m_err:
            ok = StatusService.set_note(self.repo, "SSTI Playground",
                                        remove=True)
        self.assertFalse(ok)
        m_ok.assert_not_called(), "gỡ-note skip cũng không được in ✔"
        self.assertTrue(m_err.called)

    def test_update_tags_skip_khong_duoc_success_phai_canh_bao_loi(self):
        with _rig_update_status_vanish(), \
             mock.patch.object(Logger, "success") as m_ok, \
             mock.patch.object(Logger, "error") as m_err:
            ok, rejected = StatusService.update_tags(
                self.repo, "SSTI Playground", ["hard"])
        self.assertFalse(ok)
        self.assertEqual(rejected, [])
        m_ok.assert_not_called(), "tag skip không được in 🏷️ success"
        self.assertTrue(m_err.called)
        msg = " ".join(str(c.args[0]) for c in m_err.call_args_list)
        self.assertIn("SSTI Playground", msg)

    # ---- NOOP: giá trị cũ == giá trị mới ------------------------------ #
    def test_set_note_noop_thong_dieu_trung_tinh_khong_success(self):
        self.assertTrue(StatusService.set_note(
            self.repo, "SSTI Playground", text="same note"))
        mp = self._meta("ssti_playground")
        before_raw = mp.read_bytes()
        ino = mp.stat().st_ino
        with mock.patch.object(Logger, "success") as m_ok, \
             mock.patch.object(Logger, "info") as m_info, \
             mock.patch.object(Logger, "warning") as m_warn, \
             mock.patch.object(Logger, "error") as m_err:
            ok = StatusService.set_note(
                self.repo, "SSTI Playground", text="same note")
        self.assertTrue(ok, "noop là thao tác hợp lệ, không phải lỗi")
        m_ok.assert_not_called(), "noop không được in ✔ success"
        m_warn.assert_not_called()
        m_err.assert_not_called()
        self.assertTrue(m_info.called, "noop phải có thông điệp trung tính")
        self.assertEqual(mp.read_bytes(), before_raw,
                         "noop không được rewrite file")
        self.assertEqual(mp.stat().st_ino, ino)

    def test_remove_note_lan_thu_hai_la_noop_trung_tinh(self):
        mp = self._meta("ssti_advanced")
        self.repo.update_status(mp, lambda st: {**st, "notes": "temp"})
        self.assertTrue(StatusService.set_note(self.repo, "2", remove=True))
        before_raw = mp.read_bytes()
        with mock.patch.object(Logger, "success") as m_ok, \
             mock.patch.object(Logger, "info") as m_info:
            ok = StatusService.set_note(self.repo, "2", remove=True)
        self.assertTrue(ok)
        m_ok.assert_not_called()
        self.assertTrue(m_info.called)
        self.assertEqual(mp.read_bytes(), before_raw)

    def test_update_tags_noop_thong_dieu_trung_tinh_khong_success(self):
        self.assertTrue(StatusService.update_tags(
            self.repo, "SSTI Playground", ["hard"])[0])
        mp = self._meta("ssti_playground")
        before_raw = mp.read_bytes()
        with mock.patch.object(Logger, "success") as m_ok, \
             mock.patch.object(Logger, "info") as m_info:
            ok, _rej = StatusService.update_tags(
                self.repo, "SSTI Playground", ["hard"])
        self.assertTrue(ok)
        m_ok.assert_not_called(), "thêm tag đã có -> noop, không ✔"
        self.assertTrue(m_info.called)
        self.assertEqual(mp.read_bytes(), before_raw)


class TestF2HoardRemoveSignal(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c16hoard_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.root = Path(self._tmp) / "ws"
        _make_ws(self.root)
        self.repo = WorkspaceRepo(self.root)

    def _args(self, target: str) -> argparse.Namespace:
        return argparse.Namespace(workspace=str(self.root), id=None,
                                  name=None, target=target, remove=True)

    def test_go_flag_skip_khong_duoc_success_va_exit_nonzero(self):
        from ctf_downloader.cli_commands import _handle_hoard_remove
        with _rig_update_status_vanish(), \
             mock.patch.object(Logger, "success") as m_ok, \
             mock.patch.object(Logger, "error") as m_err:
            with self.assertRaises(SystemExit) as cm:
                _handle_hoard_remove(self._args("SSTI Playground"))
        self.assertEqual(cm.exception.code, 1,
                         "ghi bị skip phải thất bại rõ (exit 1)")
        m_ok.assert_not_called(), "KHÔNG được in 🗑 success khi ghi bị skip"
        self.assertTrue(m_err.called)
        msg = " ".join(str(c.args[0]) for c in m_err.call_args_list)
        self.assertIn("SSTI Playground", msg)

    def test_go_flag_khi_khong_co_flag_la_noop_trung_tinh(self):
        from ctf_downloader.cli_commands import _handle_hoard_remove
        mp = self.root / "Web" / "ssti_playground" / "metadata.json"
        self.repo.update_status(mp, lambda st: st)   # prime materialize v2
        before_raw = mp.read_bytes()
        with mock.patch.object(Logger, "success") as m_ok, \
             mock.patch.object(Logger, "info") as m_info:
            _handle_hoard_remove(self._args("SSTI Playground"))  # không exit
        m_ok.assert_not_called(), "không có flag để gỡ -> trung tính, không ✔"
        self.assertTrue(m_info.called)
        self.assertEqual(mp.read_bytes(), before_raw, "noop không rewrite")


# ----------------------------------------------------------------------
# F3 [LOW] — sync_solve_attribution: chỉ đếm updated khi persist thật
# ----------------------------------------------------------------------

class TestF3SyncAttributionCount(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir(parents=True)
        for cid in (21, 22):
            d = self.root / "web" / f"chal{cid}"
            d.mkdir(parents=True)
            (d / "metadata.json").write_text(json.dumps(
                {"id": cid, "name": f"Chal{cid}", "category": "web",
                 "points": 100}), encoding="utf-8")

    def _meta(self, cid: int) -> Path:
        return self.root / "web" / f"chal{cid}" / "metadata.json"

    @staticmethod
    def _platform():
        plat = types.SimpleNamespace()
        plat.fetch_solve_attribution = (
            lambda ids: {i: {"by_me": True, "by_team": False} for i in ids})
        return plat

    def repo_read(self, cid):
        return WorkspaceRepo(self.root).read_status(self._meta(cid))

    def test_persist_that_counts_and_stamps(self):
        updated = PullService.sync_solve_attribution(
            self._platform(), str(self.root))
        self.assertEqual(updated, 2)
        for cid in (21, 22):
            st = self.repo_read(cid)
            self.assertEqual(st["solve"], "solved_by_me")
            self.assertTrue(st["synced_at"], "nâng solve thật phải stamp")

    def test_skip_khong_diem_updated(self):
        class SkipRepo(WorkspaceRepo):
            def update_status(self, mp, mut):
                mp = Path(mp)
                if json.loads(mp.read_text(encoding="utf-8-sig")).get("id") == 21:
                    return StatusWriteResult({}, noop=False, persisted=False)
                return super().update_status(mp, mut)

        # Prime chal21 lên 'working' (không phải 'unsolved'): nếu code cũ
        # chỉ so solve trong kết quả trả về thì skip (trả state rỗng ->
        # 'unsolved') vẫn bị đếm nhầm là updated — đúng phantom cần chặn.
        WorkspaceRepo(self.root).update_status(
            self._meta(21), lambda st: {**st, "solve": "working"})

        with mock.patch.object(wr_mod, "WorkspaceRepo", SkipRepo):
            updated = PullService.sync_solve_attribution(
                self._platform(), str(self.root))
        self.assertEqual(updated, 1,
                         "challenge bị SKIP (persisted=False) không được đếm")

    def test_noop_khong_diem_updated(self):
        class NoopRepo(WorkspaceRepo):
            def update_status(self, mp, mut):
                # Tiến trình khác vừa nâng solve trước ta (race) -> mutator
                # thành no-op: KHÔNG ghi, không đếm.
                return StatusWriteResult(
                    normalize_status({"solve": "solved_by_me"}),
                    noop=True, persisted=False)

        with mock.patch.object(wr_mod, "WorkspaceRepo", NoopRepo):
            updated = PullService.sync_solve_attribution(
                self._platform(), str(self.root))
        self.assertEqual(updated, 0, "noop không được đếm là updated")


if __name__ == "__main__":
    unittest.main(verbosity=2)
