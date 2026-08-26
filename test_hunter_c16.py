"""HUNTER cycle-16 — 5 finding đã được hunter xác nhận bằng repro thực tế:

1. BUG-C16-1 [MED] storage/fileio.py — ``p.parent.mkdir(parents=True)``
   trong locked_write_text / locked_path / locked_update_json HỒI SINH
   thư mục challenge đã bị xoá giữa chừng khi sync đang chạy → sinh
   metadata.json zombie chỉ chứa field mutator set (mất id/name); nhánh
   removed còn tạo ghost ``{"removed_from_server": true}``. Fix: parent
   biến mất tại lúc ghi → bỏ ghi + tín hiệu skip; KHÔNG mkdir lại. mkdir
   cho lần tạo ĐẦU TIÊN hợp lệ vẫn do caller đảm nhiệm (WorkspaceBuilder
   tự os.makedirs trước khi locked_write_text — workspace_builder.py:96).

2. BUG-C16-2 [MED] workspace_repo.update_status — luôn rewrite file +
   stamp ``updated_at`` kể cả khi mutator không đổi gì (trigger thật:
   ``instance --sync`` chạy 2 lần, mirror ``container='running'`` trên
   giá trị cũ vẫn rewrite). Fix: so kết quả merge với state cũ trên đĩa;
   không đổi gì → không ghi, không đụng updated_at, báo no-op.

3. BUG-C16-3 [MED] workspace_repo.save_submit_history — thay WHOLE list
   từ snapshot caller: 2 tiến trình submit song song làm mất entry
   (P1 snapshot [A], P2 persist [A,C], P1 save [A,B] → mất C). Fix:
   đọc-lại TRONG lock rồi merge theo key duy nhất của entry (flag),
   caller-version-wins per key; API full-list giữ nguyên.

4. BUG-C16-4 [LOW] update_status đọc lại metadata NGOÀI khóa sau khi
   ghi rồi trả state đó — pull_service.py:439 đếm ``updated`` từ giá trị
   này → phantom count nếu tiến trình khác ghi giữa hai bước. Fix: trả
   về state đã merge TRONG lock.

5. BUG-C16-5 [LOW] pull_service.sync_workspace — early-return ok:False
   thiếu keys removed_local/corrupt_local/write_errors có ở shape thành
   công → consumer truy cập trực tiếp bị KeyError. Fix: bổ sung đủ keys.

Quy ước hunter: test FAIL có chủ ý = bug thật đang tái hiện
(BUG-C16-*); PASS = documentation hành vi sau fix.
Chạy: python3 test_hunter_c16.py -v
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.storage.fileio import (
    atomic_write_text,
    locked_path,
    locked_update_json,
    locked_write_text,
)
from ctf_downloader.storage.workspace_repo import WorkspaceRepo
from ctf_downloader.services.pull_service import PullService


def _inode(path) -> int:
    return os.stat(path).st_ino


class TempWSCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ws"
        self.root.mkdir(parents=True)
        self.repo = WorkspaceRepo(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def make_challenge(self, cid=1, name="Alpha", cat="web"):
        d = self.root / cat / name.lower()
        d.mkdir(parents=True, exist_ok=True)
        mp = d / "metadata.json"
        mp.write_text(json.dumps({"id": cid, "name": name, "category": cat,
                                  "points": 100}), encoding="utf-8")
        return mp


# ----------------------------------------------------------------------
# BUG-C16-1: locked helpers KHÔNG được hồi sinh thư mục đã bị xoá
# ----------------------------------------------------------------------

class TestF1NoResurrect(TempWSCase):
    def test_locked_update_json_skip_when_parent_deleted(self):
        mp = self.make_challenge()
        shutil.rmtree(mp.parent)
        out = locked_update_json(mp, lambda d: {**(d or {}), "synced": True})
        # Tín hiệu skip: không có dict "thành công giả" trả về.
        self.assertIsNone(out,
                          "parent biến mất phải trả None (skip), không ghi")
        self.assertFalse(mp.parent.exists(),
                         "mkdir(parents=True) đã HỒI SINH thư mục challenge")
        self.assertFalse(mp.exists(), "metadata.json zombie không được sinh")

    def test_locked_update_json_still_creates_file_in_existing_dir(self):
        d = self.root / "web" / "beta"
        d.mkdir(parents=True)
        mp = d / "metadata.json"   # dir có sẵn, file chưa có — lần tạo đầu
        out = locked_update_json(mp, lambda dd: {**(dd or {}), "id": 9})
        self.assertEqual(out.get("id"), 9)
        self.assertTrue(mp.exists())

    def test_locked_update_json_skip_sentinel_leaves_file_untouched(self):
        from ctf_downloader.storage.fileio import SKIP_WRITE
        mp = self.make_challenge()
        before = mp.read_bytes()
        out = locked_update_json(mp, lambda d: SKIP_WRITE)
        self.assertIsNone(out)
        self.assertEqual(mp.read_bytes(), before, "SKIP_WRITE phải bỏ qua ghi")

    def test_locked_write_text_skip_when_parent_deleted(self):
        target = self.root / "gone" / "RANKING.md"
        (self.root / "gone").mkdir()
        locked_write_text(target, "v1")
        shutil.rmtree(self.root / "gone")
        wrote = locked_write_text(target, "v2")
        self.assertFalse(wrote, "parent biến mất -> skip (False)")
        self.assertFalse(target.parent.exists(), "không được hồi sinh 'gone'")
        self.assertFalse(target.exists())

    def test_locked_write_text_ok_in_existing_dir(self):
        target = self.root / "RANKING.md"
        self.assertTrue(locked_write_text(target, "v1"))
        self.assertEqual(target.read_text(encoding="utf-8"), "v1")

    def test_locked_path_does_not_resurrect_directory(self):
        target = self.root / "vanished" / "SUMMARY.md"
        (self.root / "vanished").mkdir()
        with locked_path(target):
            atomic_write_text(target, "# v1")
        shutil.rmtree(self.root / "vanished")
        raised = False
        try:
            with locked_path(target):
                atomic_write_text(target, "# v2")
        except FileNotFoundError:
            raised = True
        self.assertTrue(raised, "ghi vào dir đã mất phải fail LOÁ (không zombie)")
        self.assertFalse(target.parent.exists(),
                         "locked_path không được mkdir lại thư mục xoá")

    def test_repo_update_status_no_zombie_on_deleted_challenge(self):
        mp = self.make_challenge(cid=7, name="Ghost", cat="pwn")
        shutil.rmtree(mp.parent)
        try:
            self.repo.update_status(mp, lambda st: {**st, "notes": "x"})
        except OSError:
            pass   # caller được phép biết là lỗi; điều kiện CỨNG là dưới đây
        self.assertFalse(mp.parent.exists(), "repo không được hồi sinh dir")
        self.assertFalse(mp.exists(), "không được sinh metadata.json zombie")

    def test_mark_removed_no_ghost_on_deleted_challenge(self):
        mp = self.make_challenge(cid=8, name="Ghost2", cat="rev")
        shutil.rmtree(mp.parent)
        PullService._mark_removed_from_server(self.repo, mp)
        self.assertFalse(mp.parent.exists())
        self.assertFalse(mp.exists(), "ghost removed_from_server không id/name")


# ----------------------------------------------------------------------
# BUG-C16-2: update_status no-op không ghi, không đụng updated_at
# ----------------------------------------------------------------------

class TestF2UpdateStatusNoop(TempWSCase):
    def test_second_identical_container_mirror_is_noop(self):
        mp = self.make_challenge()
        # Lần 1: none -> running (real change) — phải ghi.
        out1 = self.repo.update_status(
            mp, lambda st: {**st, "container": "running"})
        self.assertFalse(getattr(out1, "noop", False))
        raw1 = mp.read_bytes()
        ino1 = _inode(mp)
        stamp1 = json.loads(raw1)["status"]["updated_at"]
        self.assertTrue(stamp1)
        # Lần 2 (--sync chạy lại, container vẫn 'running'): KHÔNG ghi.
        out2 = self.repo.update_status(
            mp, lambda st: {**st, "container": "running"})
        self.assertTrue(getattr(out2, "noop", False),
                        "mutator không đổi gì phải được báo no-op")
        self.assertEqual(mp.read_bytes(), raw1,
                         "no-op không được rewrite file")
        self.assertEqual(_inode(mp), ino1)
        self.assertEqual(json.loads(mp.read_bytes())["status"]["updated_at"],
                         stamp1, "no-op không được đụng updated_at")

    def test_in_place_scalar_mutator_noop_detected(self):
        """Mutator style gán in-place (instance_service._mut_container):
        gán cùng giá trị cũ vẫn phải là no-op."""
        mp = self.make_challenge()

        def mut_cont(st):
            st["container"] = "running"
            return st

        self.repo.update_status(mp, mut_cont)     # none -> running: ghi
        raw1 = mp.read_bytes()
        out = self.repo.update_status(mp, mut_cont)   # running -> running
        self.assertTrue(getattr(out, "noop", False))
        self.assertEqual(mp.read_bytes(), raw1)

    def test_real_change_still_writes_and_reports_not_noop(self):
        mp = self.make_challenge()
        out = self.repo.update_status(
            mp, lambda st: {**st, "labels": st["labels"] + ["x"]})
        self.assertFalse(getattr(out, "noop", False))
        ino1 = _inode(mp)
        out = self.repo.update_status(
            mp, lambda st: {**st, "labels": st["labels"] + ["y"]})
        self.assertFalse(getattr(out, "noop", False))
        self.assertNotEqual(_inode(mp), ino1, "đổi thật phải ghi (inode mới)")

    def test_identity_mutator_still_materializes_legacy_status(self):
        """Priming migrate-on-read (test_sync.py phụ thuộc): metadata legacy
        KHÔNG có block status + identity mutator -> file PHẢI được materialize
        schema v2 (đây là thay đổi thật so với nội dung trên đĩa)."""
        mp = self.make_challenge()
        out = self.repo.update_status(mp, lambda st: st)
        on_disk = json.loads(mp.read_text(encoding="utf-8"))
        self.assertIn("status", on_disk, "materialize schema v2 phải diễn ra")
        self.assertEqual(on_disk["status"]["schema_version"], 2)
        self.assertFalse(getattr(out, "noop", False))

    def test_return_value_is_dict_compatible(self):
        mp = self.make_challenge()
        out = self.repo.update_status(
            mp, lambda st: {**st, "solve": "solved_by_me"})
        # Consumer hiện tại dùng như dict thuần:
        self.assertEqual(out["solve"], "solved_by_me")
        self.assertIsInstance(dict(out), dict)
        self.assertEqual(out.get("solve"), "solved_by_me")


# ----------------------------------------------------------------------
# BUG-C16-3: save_submit_history merge-theo-key trong lock
# ----------------------------------------------------------------------

class TestF3SubmitHistoryMerge(TempWSCase):
    def test_lost_entry_repro_two_processes(self):
        """P1 snapshot [A] | P2 persist [A,C] | P1 save [A,B] -> C phải sống."""
        a = {"flag": "FLAG{a}", "result": "correct"}
        b = {"flag": "FLAG{b}", "result": "correct"}
        c = {"flag": "FLAG{c}", "result": "wrong"}
        self.repo.save_submit_history({"entries": [dict(a)]})       # disk [A]
        self.repo.save_submit_history(                              # P2 [A,C]
            {"entries": [dict(a), dict(c)]})
        self.repo.save_submit_history({"entries": [dict(a), dict(b)]})  # P1
        loaded = self.repo.load_submit_history()["entries"]
        flags = {e["flag"] for e in loaded}
        self.assertEqual(flags, {"FLAG{a}", "FLAG{b}", "FLAG{c}"},
                         "entry của tiến trình khác không được bị xoá")

    def test_caller_version_wins_per_key(self):
        old = {"flag": "FLAG{x}", "result": "wrong"}
        new = {"flag": "FLAG{x}", "result": "correct"}
        self.repo.save_submit_history({"entries": [dict(old)]})
        self.repo.save_submit_history({"entries": [dict(new)]})
        entries = self.repo.load_submit_history()["entries"]
        xs = [e for e in entries if e["flag"] == "FLAG{x}"]
        self.assertEqual(len(xs), 1, "cùng flag phải upsert, không nhân đôi")
        self.assertEqual(xs[0]["result"], "correct")

    def test_roundtrip_api_compat_full_list(self):
        entries = [{"flag": f"FLAG{{{i}}}", "result": "correct"}
                   for i in range(5)]
        self.repo.save_submit_history({"entries": [dict(e) for e in entries]})
        self.assertEqual(self.repo.load_submit_history()["entries"], entries)

    def test_entries_without_flag_preserved_once(self):
        nf1 = {"challenge_id": 1, "result": "correct"}
        nf2 = {"challenge_id": 2, "result": "wrong"}
        self.repo.save_submit_history({"entries": [dict(nf1), dict(nf2)]})
        self.repo.save_submit_history({"entries": [dict(nf1)]})
        entries = self.repo.load_submit_history()["entries"]
        self.assertEqual(len(entries), 2, "entry không flag phải được giữ")
        ids = sorted(e["challenge_id"] for e in entries)
        self.assertEqual(ids, [1, 2])

    def test_merge_preserves_chronological_order_for_tail(self):
        """`ctf history --tail` lấy entries[-N:] là "mới nhất" — merge phải
        thay entry cùng khóa TẠI CHỖ và nối entry mới vào CUỐI, không xáo
        trộn thứ tự thời gian."""
        a = {"flag": "FLAG{a}", "result": "correct"}
        b = {"flag": "FLAG{b}", "result": "correct"}
        self.repo.save_submit_history({"entries": [dict(a), dict(b)]})
        # Tiến trình khác thêm C sau B:
        self.repo.save_submit_history(
            {"entries": [dict(a), dict(b),
                         {"flag": "FLAG{c}", "result": "wrong"}]})
        # P1 snapshot stale [A,B] cập nhật A thành correct->wrong + thêm D:
        self.repo.save_submit_history({"entries": [
            {"flag": "FLAG{a}", "result": "wrong"},
            dict(b),
            {"flag": "FLAG{d}", "result": "correct"}]})
        flags = [e["flag"] for e in self.repo.load_submit_history()["entries"]]
        self.assertEqual(flags, ["FLAG{a}", "FLAG{b}", "FLAG{c}", "FLAG{d}"])

    def test_multiprocess_parallel_submits_no_loss(self):
        """2 process submit THẬT song song: mỗi cái load 1 lần đầu (snapshot
        stale cố tình) rồi save-full-list 10 lần với flag riêng — tất cả
        20 flag phải còn nguyên."""
        procs = []
        for tag in ("P1", "P2"):
            worker = (
                "import sys, time\n"
                f"sys.path.insert(0, {os.getcwd()!r})\n"
                "from ctf_downloader.storage.workspace_repo import WorkspaceRepo\n"
                f"repo = WorkspaceRepo({str(self.root)!r})\n"
                f"tag = {tag!r}\n"
                "snap = repo.load_submit_history()['entries']\n"
                "for i in range(10):\n"
                "    e = {'flag': f'FLAG{{{tag}-{i}}}', 'result': 'correct'}\n"
                "    repo.save_submit_history({'entries': snap + [e]})\n"
            )
            procs.append(subprocess.Popen([sys.executable, "-c", worker]))
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        flags = {e["flag"]
                 for e in self.repo.load_submit_history()["entries"]}
        expected = {f"FLAG{{P{i}-{j}}}" for i in (1, 2) for j in range(10)}
        self.assertEqual(expected - flags, set(),
                         f"mất entry submit song song: {sorted(expected - flags)}")


# ----------------------------------------------------------------------
# BUG-C16-4: update_status trả state merge TRONG lock (không re-read)
# ----------------------------------------------------------------------

class TestF4InLockReturn(TempWSCase):
    def test_return_reflects_in_lock_merge_not_external_rewrite(self):
        mp = self.make_challenge()
        orig_read = self.repo.read_metadata
        calls = {"n": 0}

        def poisoned(path):
            calls["n"] += 1
            # Mô phỏng tiến trình khác ghi đè NGAY giữa read-back ngoài khóa:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            data["status"]["labels"] = ["phantom-from-other-process"]
            Path(path).write_text(json.dumps(data), encoding="utf-8")
            return orig_read(path)

        self.repo.read_metadata = poisoned
        try:
            out = self.repo.update_status(
                mp, lambda st: {**st, "labels": st["labels"] + ["mine"]})
        finally:
            self.repo.read_metadata = orig_read
        self.assertEqual(out.get("labels"), ["mine"],
                         "phải trả state merge TRONG lock, không read-back "
                         "ngoài khóa (phantom count pull_service.py:439)")


# ----------------------------------------------------------------------
# BUG-C16-5: sync_workspace early-return đủ shape keys
# ----------------------------------------------------------------------

class _DeadPlatform:
    """fetch_challenges luôn nổ — đẩy sync_workspace vào early-return."""
    platform_type = "generic"

    def fetch_challenges(self):
        raise RuntimeError("server dead")


class _EmptyPlatform(_DeadPlatform):
    def fetch_challenges(self):
        return []


class TestF5SyncEarlyReturnShape(TempWSCase):
    SHAPE_KEYS = ("ok", "updated", "new", "new_on_server", "removed_local",
                  "corrupt_local", "write_errors", "drift",
                  "unsolved_locally_solved_remotely",
                  "total_local", "total_server")

    def _check_shape(self, result):
        for key in self.SHAPE_KEYS:
            self.assertIn(key, result,
                          f"early-return thiếu key '{key}' — consumer "
                          f"truy cập trực tiếp sẽ KeyError")

    def test_error_platform_result_has_full_shape(self):
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            result = PullService.sync_workspace(self.repo, _DeadPlatform())
        self.assertFalse(result["ok"])
        self._check_shape(result)
        self.assertEqual(result["removed_local"], [])
        self.assertEqual(result["corrupt_local"], [])
        self.assertEqual(result["write_errors"], [])

    def test_empty_platform_result_has_full_shape(self):
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            result = PullService.sync_workspace(self.repo, _EmptyPlatform())
        self.assertFalse(result["ok"])
        self._check_shape(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
