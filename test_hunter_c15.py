"""HUNTER cycle-15 — bấm biên sâu 3 surface chưa từng soi kỹ:

1. PullService.sync_workspace (ctf sync 2-way) — ngoài "remote thắng field
   động" của cycle-6: local-solved vs remote-unsolved, merge per-field,
   challenge local bị xoá khỏi giải, metadata corrupt giữa sync, workspace
   read-only, IDEMPOTENCE 2 lần sync liên tiếp.
2. StorageManager.archive_workspace sau fix ENOSPC f8f94ea — regression
   cleanup, ``_archives/`` là FILE, git add fail giữa chừng để lại state gì,
   2 process archive SONG SONG cùng workspace (flock có che không?).
3. SniperService.load_targets / resolve_start — schema targets JSON: thiếu
   field, start_at rác, target trùng lặp, list rỗng, array chứa non-dict,
   delay_seconds NaN/Infinity (json stdlib nhận sẵn!).

4. Cross-check tĩnh: sync/archive/sniper còn gọi open() ghi thẳng state file
   ngoài storage layer không (pattern vi phạm cũ).

Mọi network MOCK (FakePlatform + submitter giả — không socket nào).
Quy ước hunter: test FAIL có chủ ý = bug thật đang tái hiện (đánh dấu
BUG-C15-*); PASS = documentation hành vi hiện tại là đúng/thiết kế.
Chạy: python3 test_hunter_c15.py -v
"""
import errno
import gc
import hashlib
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tarfile
import unittest
import unittest.mock
import warnings
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.services import storage_manager as sm_mod
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.services.sniper_service import SniperService
from ctf_downloader.services.storage_manager import StorageError, StorageManager
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def make_chall(cid, name, category, points=100, **kw):
    kw.setdefault("description", f"desc {name}")
    kw.setdefault("files", [])
    return Challenge(id=cid, name=name, category=category, points=points, **kw)


def build_ws(root):
    """Workspace tối thiểu: web/alpha (id=1) + pwn/beta (id=2)."""
    repo = WorkspaceRepo(root)
    for cid, name, cat in ((1, "Alpha", "web"), (2, "Beta", "pwn")):
        d = os.path.join(root, cat, name.lower())
        for sub in ("challenge", "solver", "writeup"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        repo.write_metadata(os.path.join(d, "metadata.json"),
                            {"id": cid, "name": name, "category": cat,
                             "points": 100})
        Path(d, "challenge", "README.md").write_text(f"# {name}\n")
        Path(d, "writeup", "README.md").write_text(f"# wu {name}\n")
    return repo


class FakePlatform:
    platform_type = "generic"

    def __init__(self, challenges, attr_map=None):
        self.ctf_info = CTFInfo(title="C15", url="https://x.example.com",
                                platform_type=self.platform_type)
        self.ctf_info.challenges = list(challenges)
        self._challenges = list(challenges)
        self.attr_map = dict(attr_map or {})

    def authenticate(self):
        return True

    def fetch_challenges(self):
        return list(self._challenges)

    def fetch_solve_attribution(self, ids):
        return {i: self.attr_map.get(i) for i in ids}


class FakeSubmitter:
    """Submitter giả: luôn correct, đếm số phát bắn."""
    def __init__(self):
        self.submit_history = []
        self.platform = None
        self.calls = []

    def submit(self, challenge, flag, force=False):
        self.calls.append((challenge, flag, force))
        self.submit_history.append({"flag": flag, "result": "correct"})
        return True, "Correct!"


class SyncBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c15_sync_")
        self.ws = os.path.join(self._tmp, "ws")
        self.repo = build_ws(self.ws)

    def tearDown(self):
        # phòng hờ: trả quyền ghi trước khi dọn (case read-only)
        for dirpath, dirnames, _f in os.walk(self._tmp):
            os.chmod(dirpath, 0o755)
        shutil.rmtree(self._tmp, ignore_errors=True)

    def meta_path_of(self, cid):
        for mp in self.repo.iter_challenges():
            if str(self.repo.read_metadata(mp).get("id")) == str(cid):
                return mp
        self.fail(f"không có metadata id={cid}")

    def quiet_sync(self, platform):
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            res = PullService.sync_workspace(self.repo, platform)
        return res


# ----------------------------------------------------------------------
# CASE A — sync_service sâu
# ----------------------------------------------------------------------

class TestCaseALocalStateMaster(SyncBase):
    """A1/A2 — LOCAL STATE LÀ CHỦ: solved/note giữ nguyên khi remote im."""

    def test_a1_local_solved_remote_unsolved_keeps_local_no_drift(self):
        beta = self.meta_path_of(2)
        self.repo.update_status(
            beta, lambda st: {**st, "solve": "solved_by_me",
                              "notes": "tự solve được rồi"})
        plat = FakePlatform([make_chall(1, "Alpha", "web"),
                             make_chall(2, "Beta", "pwn", points=150)],
                            attr_map={})   # remote: chưa ai solve
        res = self.quiet_sync(plat)
        self.assertTrue(res["ok"])
        st = self.repo.read_status(beta)
        self.assertEqual(st["solve"], "solved_by_me",
                         "remote unsolved KHÔNG được hạ local solved")
        self.assertEqual(st["notes"], "tự solve được rồi")
        self.assertEqual(res["drift"], [])
        # PASS — documentation: hướng đi đúng.

    def test_a2_draft_note_intact_per_field_when_remote_changes_dynamic(self):
        alpha = self.meta_path_of(1)
        self.repo.update_status(alpha, lambda st: {
            **st, "notes": "draft wip", "labels": ["todo"],
            "flag": {"value": "FLAG{draft}", "state": "found_unverified"}})
        plat = FakePlatform([make_chall(1, "Alpha", "web", points=777),
                             make_chall(2, "Beta", "pwn")],
                            attr_map={2: {"by_team": True,
                                          "solver_names": ["mate"]}})
        res = self.quiet_sync(plat)
        meta = self.repo.read_metadata(alpha)
        st = self.repo.read_status(alpha)
        self.assertEqual(meta["points"], 777, "field động remote thắng")
        self.assertEqual(st["notes"], "draft wip")
        self.assertEqual(st["labels"], ["todo"])
        self.assertEqual(st["flag"]["value"], "FLAG{draft}")
        self.assertEqual(st["solve"], "unsolved",
                         "attribution của challenge KHÁC không lan sang")
        # drift chỉ Beta (server solved, local unsolved) và KHÔNG tự sửa
        self.assertEqual([d["id"] for d in res["drift"]], [2])
        self.assertEqual(self.repo.read_status(self.meta_path_of(2))["solve"],
                         "unsolved")
        # PASS — merge per-field đúng thiết kế.


class TestCaseBLocalOnlyAndCorrupt(SyncBase):
    """A3/A4 — challenge bị xoá khỏi giải + metadata corrupt giữa sync."""

    def test_b1_local_only_id_not_on_server_is_silent_total_mismatch_only(self):
        # BUG-C15-3: id 999 chỉ tồn tại local (đã bị xoá khỏi giải).
        # run_update (--update) đánh dấu removed_from_server=true, nhưng
        # `ctf sync` KHÔNG mark, KHÔNG liệt kê — chỉ lệch số total_local vs
        # total_server mà user phải tự trừ nhẩm.
        ghost_dir = os.path.join(self.ws, "misc", "ghost")
        os.makedirs(ghost_dir, exist_ok=True)
        self.repo.write_metadata(os.path.join(ghost_dir, "metadata.json"),
                                 {"id": 999, "name": "Ghost", "category": "misc"})
        # challenges.json cũng còn tham chiếu id 999
        self.repo.write_challenges({"challenges": [
            {"id": 1, "name": "Alpha"}, {"id": 999, "name": "Ghost"}]})
        plat = FakePlatform([make_chall(1, "Alpha", "web"),
                             make_chall(2, "Beta", "pwn")])
        res = self.quiet_sync(plat)
        self.assertTrue(res["ok"])
        self.assertEqual(res["total_local"], 3)
        self.assertEqual(res["total_server"], 2)
        meta_ghost = self.repo.read_metadata(self.meta_path_of(999))
        marked = ("removed_from_server" in meta_ghost
                  or (meta_ghost.get("status") or {}).get("removed_from_server"))
        # EXPECTED ĐÚNG: có tín hiệu removed (như --update) hoặc ít nhất
        # result['removed_local'] liệt kê ra. HIỆN TẠI: không có gì.
        self.assertTrue(marked or res.get("removed_local"),
                        "BUG-C15-3: sync bỏ mặc challenge đã bị xoá khỏi "
                        f"giải — meta={ {k: meta_ghost.get(k) for k in ('id', 'removed_from_server')} }")

    def test_b2_corrupt_metadata_skipped_and_demoted_to_new_on_server(self):
        # metadata Alpha hỏng giữa chừng: không crash — nhưng challenge biến
        # mất khỏi local_index nên bị quảng cáo "mới trên server".
        mp = self.meta_path_of(1)
        Path(mp).write_text("{corrupt json!!", encoding="utf-8")
        plat = FakePlatform([make_chall(1, "Alpha", "web"),
                             make_chall(2, "Beta", "pwn", points=200)])
        res = self.quiet_sync(plat)     # không được nổ exception
        self.assertFalse(res["ok"],
                         "metadata corrupt phải làm sync partial-failure")
        self.assertEqual(res["new_on_server"],
                         [{"id": 1, "name": "Alpha", "category": "web"}],
                         "BUG-C15-4: metadata corrupt → challenge bị coi là "
                         "'mới trên server', gợi ý --update sẽ dựng lại dir "
                         "(ghi đè) thay vì cảnh báo file hỏng")
        self.assertFalse(list(Path(self.ws).rglob("metadata.json.bak")),
                         "read_metadata KHÔNG backup .bak như đường "
                         "challenges.json — hỏng là mất im lặng")
        # Beta vẫn merge bình thường
        self.assertEqual(self.repo.read_metadata(self.meta_path_of(2))["points"], 200)


class TestCaseCReadOnly(SyncBase):
    """A5 — workspace read-only: sync báo thành công im lặng?"""

    def test_c1_readonly_ws_reports_ok_true_updated_zero_no_error_signal(self):
        for name in ("web/alpha", "pwn/beta"):
            os.chmod(os.path.join(self.ws, name), 0o555)
        plat = FakePlatform([make_chall(1, "Alpha", "web", points=999),
                             make_chall(2, "Beta", "pwn", points=999)])
        try:
            res = self.quiet_sync(plat)   # không được nổ — nhưng phải báo lỗi
        finally:
            for name in ("web/alpha", "pwn/beta"):
                os.chmod(os.path.join(self.ws, name), 0o755)
        self.assertFalse(res["ok"],
                         "persist error phải làm CLI sync exit non-zero")
        self.assertEqual(res["updated"], 0)
        # EXPECTED ĐÚNG: có tín hiệu lỗi ghi (write_errors / ok=False /
        # warning). HIỆN TẠI: ok=True sạch sẽ — user tưởng sync thành công.
        self.assertTrue(res.get("write_errors")
                        or res.get("errors")
                        or res.get("ok") is False,
                        "BUG-C15-1: sync trên workspace read-only vẫn "
                        f"ok=True updated=0, không một tín hiệu thất bại: {res}")


class TestCaseDIdempotent(SyncBase):
    """A6 — 2 lần sync liên tiếp, dữ liệu không đổi: có ghi file lần 2?"""

    def test_d1_second_noop_sync_still_rewrites_every_metadata_file(self):
        plat = FakePlatform([make_chall(1, "Alpha", "web"),
                             make_chall(2, "Beta", "pwn")], attr_map={})
        self.quiet_sync(plat)
        snap1 = {}
        for mp in self.repo.iter_challenges():
            st = os.stat(mp)
            snap1[str(mp)] = (st.st_ino, st.st_mtime_ns,
                              hashlib.sha256(Path(mp).read_bytes()).hexdigest())
        time.sleep(1.1)                    # vượt qua ranh giới giây ISO
        res2 = self.quiet_sync(plat)       # không có gì thay đổi
        self.assertEqual(res2["updated"], 0)
        rewritten = []
        for mp in self.repo.iter_challenges():
            st = os.stat(mp)
            key = str(mp)
            if (st.st_ino, st.st_mtime_ns) != snap1[key][:2]:
                rewritten.append(key)
        # EXPECTED ĐÚNG: updated=0 ⇒ không ghi lần 2 (idempotent).
        self.assertEqual(rewritten, [],
                         "BUG-C15-2: sync không idempotent — lần 2 không đổi "
                         f"gi gì mà vẫn rewrite {len(rewritten)} metadata.json "
                         "(update_status stamp updated_at/synced_at vô điều kiện)")
        # Chứng minh nội dung cũng đổi (timestamp mới) — ô nhiễm updated_at:
        for mp in self.repo.iter_challenges():
            data = json.loads(Path(mp).read_text())
            self.assertIn("synced_at", data.get("status") or data)


# ----------------------------------------------------------------------
# CASE E — storage archive sau fix f8f94ea
# ----------------------------------------------------------------------

class ArchiveBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c15_arch_")
        self.base = Path(self._tmp)
        self.ws = self.base / "myws"
        (self.ws / "sub").mkdir(parents=True)
        for i in range(5):
            (self.ws / "sub" / f"f{i}.txt").write_text(f"data{i}" * 40)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestCaseEEnospcRegression(ArchiveBase):
    def test_e1_enospc_mid_tar_cleans_partial_and_raises_storageerror(self):
        """Regression f8f94ea (C8-1): ENOSPC giữa chừng phải unlink tar.gz
        nửa chừng + raise StorageError."""
        real_open = tarfile.open

        class BoomAfterTwo:
            def __init__(self, tf):
                self._tf, self._n = tf, 0

            def add(self, *a, **k):
                self._n += 1
                if self._n > 2:
                    raise OSError(errno.ENOSPC, "No space left on device")
                return self._tf.add(*a, **k)

            def __getattr__(self, item):
                return getattr(self._tf, item)

            def __enter__(self):
                return self          # with-block phải cầm WRAPPER để bắn nổ

            def __exit__(self, *exc):
                return self._tf.__exit__(*exc)

        def fake_open(name, mode="r", **kw):
            tf = real_open(name, mode, **kw)
            return BoomAfterTwo(tf) if mode == "w:gz" else tf

        dest = self.base / "_archives"
        with unittest.mock.patch.object(sm_mod.tarfile, "open", fake_open):
            with self.assertRaises(StorageError):
                StorageManager.archive_workspace(self.ws, out_dir=dest)
        leftovers = [p.name for p in dest.iterdir()]
        self.assertEqual(leftovers, [],
                         f"tar.gz nửa chừng không được dọn: {leftovers}")
        # PASS — fix f8f94ea vẫn sống sau các commit sau đó.


class TestCaseFArchivesIsFile(ArchiveBase):
    def test_f1_archives_path_is_file_raises_storageerror_contract(self):
        (self.base / "_archives").write_text("not a dir", encoding="utf-8")
        try:
            StorageManager.archive_workspace(self.ws, out_dir=self.base / "_archives")
            self.fail("phải raise")
        except StorageError:
            pass                       # đúng hợp đồng dịch vụ
        except Exception as exc:
            # BUG-C15-5: mkdir(exist_ok=True) gặp FILE → FileExistsError thoát
            # RAW, vi phạm hợp đồng "lỗi storage → StorageError" (CLI bắt
            # Exception chung nên UX còn ổn, nhưng caller thư mục khác vỡ).
            self.assertIsInstance(exc, StorageError,
                                  f"BUG-C15-5: {_type(exc)} thay vì StorageError")


def _type(exc):
    return type(exc).__name__


class TestCaseEGitFail(ArchiveBase):
    def test_g1_git_add_fail_raises_and_leaves_initialized_repo_plus_remote(self):
        """git add fail giữa chừng: raise StorageError đúng, nhưng state dở
        dang gì còn lại trong out_dir?"""
        dest = self.base / "_archives"
        real_run_git = StorageManager._run_git

        def spy(args, cwd):
            if args and args[0] == "add":
                raise StorageError("git add thất bại (giả lập ENOSPC)")
            return real_run_git(args, cwd)

        with unittest.mock.patch.object(StorageManager, "_run_git", staticmethod(spy)):
            with self.assertRaises(StorageError):
                StorageManager.archive_workspace(self.ws, out_dir=dest,
                                                 git_remote="https://git.example.com/bk.git")
        # State dở dang (documentation):
        self.assertTrue((dest / ".git").exists(), "repo init còn lại")
        remotes = subprocess.run(["git", "remote"], cwd=dest,
                                 capture_output=True, text=True).stdout.split()
        self.assertIn("origin", remotes, "remote origin còn trỏ ra ngoài")
        archives = [p.name for p in dest.iterdir() if p.suffix == ".gz"]
        self.assertEqual(len(archives), 1, "archive file vẫn nằm đó, chưa commit")
        commits = subprocess.run(["git", "rev-list", "--count", "HEAD"],
                                 cwd=dest, capture_output=True, text=True)
        self.assertNotEqual(commits.returncode, 0, "chưa commit nào cả")
        # PASS — raise đúng hợp đồng; residual: repo rỗng + remote + archive
        # chưa commit. Chấp nhận được nhưng nên ghi chú cho user.


class TestCaseGParallelArchive(ArchiveBase):
    """HAI writer cùng lúc vào CÙNG <name>_<YYYYMMDD>.tar.gz."""

    def test_g1_two_concurrent_archive_processes_corrupt_archive(self):
        """BUG-C15-6 (H): archive_workspace không có flock và ghi TRỰC TIẾP
        lên tên file cuối cùng ``<name>_<YYYYMMDD>.tar.gz`` trong cùng ngày
        (storage_manager.py:389-395, không tmp+rename). 2 process archive
        song song cùng workspace → hai stream gzip truncate/ghi đè lẫn nhau
        IM LẶNG, cả hai đều báo thành công.

        Khi nội dung 2 lần chạy GIỐNG HỆT thì may mắn ra file hợp lệ
        (header gzip 10 byte khớp độ dài) — nhưng nếu workspace THAY ĐỔI
        giữa lúc process A mở tar và process B chạy (bình thường trong giải:
        file mới/log sinh ra liên tục) thì hai stream KHÁC NHAU → sản phẩm
        cuối là rác. Test mô phỏng đúng kịch bản đó: slow mở archive trước,
        trong lúc nó ngủ workspace đổi (xoá f4, thêm f9), fast archive với
        bộ member mới."""
        dest = self.base / "_archives"
        entered = threading.Event()
        slow_ident = {}
        fired = {"done": False}
        real_open = tarfile.open

        def patched_open(name, mode="r", *a, **kw):
            tf = real_open(name, mode, *a, **kw)
            if mode == "w:gz" and threading.get_ident() == slow_ident["id"]:
                orig_add = tf.add

                def slow_add(*aa, **kk):
                    if not fired["done"]:
                        fired["done"] = True
                        entered.set()
                        time.sleep(0.5)   # nhường writer kia truncate+viết xong
                    return orig_add(*aa, **kk)

                tf.add = slow_add
            return tf

        results = {}

        def runner(tag):
            try:
                results[tag] = StorageManager.archive_workspace(
                    self.ws, out_dir=dest)
            except Exception as exc:
                results[tag] = {"error": f"{_type(exc)}: {exc}"}

        with unittest.mock.patch.object(sm_mod.tarfile, "open", patched_open):
            def mark_and_run():
                slow_ident["id"] = threading.get_ident()
                runner("slow")

            t_slow = threading.Thread(target=mark_and_run)
            t_slow.start()
            self.assertTrue(entered.wait(timeout=5),
                            "slow writer không kịp mở archive")
            # Workspace ĐỔI giữa 2 lần chạy — kịch bản thật khi giải đang diễn
            # ra (file mới sinh ra, file cũ bị xoá) trong lúc A còn đang tar.
            (self.ws / "sub" / "f4.txt").unlink()
            (self.ws / "sub" / "f9.txt").write_text("fresh" * 40)
            t_fast = threading.Thread(target=runner, args=("fast",))
            t_fast.start()
            t_fast.join(timeout=30)
            t_slow.join(timeout=30)

        # Cả hai TỰ TIN thành công — không một lời cảnh báo tranh chấp:
        self.assertNotIn("error", results.get("slow", {}),
                         f"slow fail: {results}")
        self.assertNotIn("error", results.get("fast", {}),
                         f"fast fail: {results}")
        archive = dest / f"myws_{time.strftime('%Y%m%d')}.tar.gz"
        self.assertTrue(archive.exists())
        expected = {"sub/f0.txt", "sub/f1.txt", "sub/f2.txt",
                    "sub/f3.txt", "sub/f9.txt"}
        got = None
        try:
            with tarfile.open(archive, "r:gz") as tf:
                got = set(tf.getnames())
        except Exception as exc:
            self.fail(
                "BUG-C15-6: 2 archive song song cùng workspace (workspace có "
                f"thay đổi ở giữa) → tar.gz hỏng hoàn toàn ({_type(exc)}: "
                "{exc}) trong khi cả hai tiến trình đều báo thành công — cần "
                "khoá (lock file như storage.fileio) hoặc tmp+rename")
        missing = expected - got
        self.assertEqual(missing, set(),
                         f"BUG-C15-6: archive 'thành công' nhưng thiếu member "
                         f"{missing} — stream bị ăn nhau im lặng")


# ----------------------------------------------------------------------
# CASE H — sniper targets JSON
# ----------------------------------------------------------------------

class SniperBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c15_sniper_")
        self.ws = Path(self._tmp)
        self.repo = WorkspaceRepo(str(self.ws))
        self.submitter = FakeSubmitter()

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def svc(self):
        return SniperService(self.repo, self.submitter)

    def write_targets(self, obj_text):
        (self.ws / "sniper.json").write_text(obj_text, encoding="utf-8")


class TestCaseHTargetSchema(SniperBase):
    def test_h1_missing_required_fields_skipped_with_warning(self):
        self.write_targets(json.dumps([
            {"challenge": "only-chall"},              # thiếu flag
            {"flag": "FLAG{x}"},                      # thiếu challenge
            {"challenge": "   ", "flag": "FLAG{y}"},  # challenge rỗng sau strip
            {"challenge": "ok", "flag": "FLAG{good}", "delay_seconds": 1},
        ]))
        targets = self.svc().load_targets()
        self.assertEqual([t["challenge"] for t in targets], ["ok"])
        # PASS — validate đẹp, không raise.

    def test_h2_array_with_non_dict_entries_skipped_not_crash(self):
        self.write_targets(json.dumps([
            "just a string", 42, None, ["nested"],
            {"challenge": "real", "flag": "F{1}"},
        ]))
        targets = self.svc().load_targets()
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["challenge"], "real")
        # PASS.

    def test_h3_empty_list_and_empty_wrapper_clean(self):
        self.write_targets("[]")
        self.assertEqual(self.svc().load_targets(), [])
        self.write_targets('{"targets": []}')
        self.assertEqual(self.svc().load_targets(), [])
        # PASS.

    def test_h4_duplicate_targets_both_returned_double_fire(self):
        """BUG-C15-7 (L): không dedup theo (challenge, flag) — target trùng
        được bắn 2 phát. Với submit thật: phát 2 lãng phí request/rủi ro
        rate-limit; nếu phát 1 sai thì blacklist ăn ngay phát 2."""
        self.write_targets(json.dumps([
            {"challenge": "dupe", "flag": "FLAG{s}"},
            {"challenge": "dupe", "flag": "FLAG{s}"},
        ]))
        targets = self.svc().load_targets()
        # EXPECTED ĐÚNG: dedup còn 1. HIỆN TẠI: 2.
        self.assertEqual(len(targets), 1,
                         f"BUG-C15-7: target trùng không được dedup: {targets}")

    def test_h5_delay_seconds_nan_inf_exotics(self):
        """BUG-C15-8 (L): json.loads stdlib chấp nhận Infinity/NaN —
        delay_seconds=Infinity qua mặt validate (max(0.0, inf)=inf), target
        KHÔNG BAO GIỜ due → run() polling vô hạn không bắn, không thoát.
        NaN thì vô hại do max(0.0, nan)===0.0."""
        self.write_targets('{"targets": ['
                           '{"challenge": "inf", "flag": "F{1}", '
                           '"delay_seconds": Infinity},'
                           '{"challenge": "nan", "flag": "F{2}", '
                           '"delay_seconds": NaN}]}')
        targets = self.svc().load_targets()
        by_ch = {str(t["challenge"]): t for t in targets}
        self.assertLessEqual(by_ch["nan"]["delay_seconds"], 0.0,
                             "NaN đáng lẽ bị chặn về 0 an toàn")
        # EXPECTED ĐÚNG: non-finite bị loại/về 0. HIỆN TẠI: inf đi xuyên qua.
        self.assertTrue(math.isfinite(by_ch["inf"]["delay_seconds"]),
                        f"BUG-C15-8: delay_seconds=Infinity được chấp nhận "
                        f"(= {by_ch['inf']['delay_seconds']}) → sniper chờ "
                        f"vô hạn, không bao giờ bắn cũng không thoát")

    def test_h7_load_targets_leaks_file_handle(self):
        """BUG-C15-9 (L): sniper_service.py:143 mở file không context manager
        — handle chỉ được GC nhặt sau, rò rỉ fd khi gọi liên tục (watch loop)."""
        self.write_targets(json.dumps([
            {"challenge": "c", "flag": "F{1}"}]))
        svc = self.svc()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            gc.collect()
            svc.load_targets()
            gc.collect()
        leaked = [w for w in caught if issubclass(w.category, ResourceWarning)
                  and "sniper.json" in str(w.message)]
        # EXPECTED ĐÚNG: không có ResourceWarning nào.
        self.assertEqual(
            leaked, [],
            f"BUG-C15-9: load_targets để rò file handle (không with/close): "
            f"{[str(w.message) for w in leaked]} — bọc ``with open(...)``")

    def test_h6_start_at_garbage_run_consumes_valueerror_clean(self):
        """start_at string rác: resolve_start raise ValueError, run() nuốt
        gọn → summary.pending đủ target, không crash, không bắn."""
        self.write_targets(json.dumps([
            {"challenge": "c1", "flag": "F{a}"},
            {"challenge": "c2", "flag": "F{b}"}]))
        svc = self.svc()
        with self.assertRaises(ValueError):
            svc.resolve_start("không-phải-ngày")
        with self.assertRaises(ValueError):
            svc.resolve_start("2026-13-45T99:99:99Z")
        summary = svc.run(poll_interval=1, start_at="không-phải-ngày")
        self.assertEqual(sorted(str(t["challenge"])
                                for t in summary["pending"]), ["c1", "c2"])
        self.assertEqual(self.submitter.calls, [], "không được bắn sớm")
        self.assertFalse(summary["aborted"])
        # PASS — xử lý sạch.


# ----------------------------------------------------------------------
# CASE I — cross-check tĩnh: ghi file ngoài storage layer
# ----------------------------------------------------------------------

class TestCaseICrossCheck(unittest.TestCase):
    def test_i1_sync_archive_sniper_have_no_raw_state_writes(self):
        """pull_service (sync), sniper_service, storage_manager KHÔNG được
        open(path,'w'/'a'/'x') ghi state — mọi ghi phải qua storage.fileio /
        WorkspaceRepo. Ngoại lệ duy nhất: sản phẩm đầu ra của chính module
        (tarfile.open archive trong storage_manager)."""
        import ctf_downloader.services as svc_pkg
        base = Path(svc_pkg.__file__).parent
        offenders = []
        for mod in ("pull_service.py", "sniper_service.py", "storage_manager.py"):
            src = (base / mod).read_text(encoding="utf-8")
            for lineno, line in enumerate(src.splitlines(), 1):
                if "open(" not in line:
                    continue
                s = line.strip()
                if s.startswith("#"):
                    continue
                is_tar_or_zip = ("tarfile.open" in line
                                 or "zipfile.ZipFile" in line)
                writes = any(tok in line for tok in ('"w"', "'w'", '"a"',
                                                     "'a'", '"x"', "'x'",
                                                     '"wb"', "'wb'"))
                if writes and not is_tar_or_zip:
                    offenders.append(f"{mod}:{lineno}: {s}")
        self.assertEqual(offenders, [],
                         "Ghi file thẳng ngoài storage layer: "
                         f"{offenders} (pattern vi phạm cũ)")
        # PASS với bộ ba này; vi phạm CÒN SÓT nằm Ở MODULE KHÁC (báo riêng):
        #   generator/workspace_builder.py:191  — open(metadata.json,'w') thẳng
        #   services/submit_service.py:692      — open(README,'w') replace flag
        #   services/instance_service.py:325,359 — write_text docs/solve


if __name__ == "__main__":
    unittest.main(verbosity=2)
