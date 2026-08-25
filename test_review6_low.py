"""Review-6 advisory LOW — 2 regression test nhỏ (TDD red→green):

1. StorageManager.archive_workspace (storage_manager.py:452-457):
   - Ctrl-C (KeyboardInterrupt là BaseException) giữa chừng không đi qua
     handler ``except OSError`` → sót file ``<archive>.lock`` trong _archives.
   - Unlink `.lock` chạy SAU khi ``locked_path`` unlock → process chờ vừa
     được grant + re-validate trên inode cũ có thể lọt vùng găng chung
     (hậu quả đã được che nhờ tmp+os.replace, nhưng cleanup phải sạch:
     unlink lockfile TRONG LÚC CÒN GIỮ KHÓA).
2. PullService.sync_workspace (pull_service.py:939-942): ``removed_local``
   bị append cả khi mark-removed raise OSError → bảng báo "removed" dù
   chưa persist (write_errors đã cảnh báo riêng).

Chạy: python3 -m pytest test_review6_low.py -q
Mọi I/O trong tmpdir; không mạng.
"""
import errno
import fcntl
import io
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.services import storage_manager as sm_mod
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.services.storage_manager import StorageError, StorageManager
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


def make_chall(cid, name, category, points=100):
    return Challenge(id=cid, name=name, category=category, points=points,
                     description=f"desc {name}", files=[])


class ArchiveBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="review6_arch_")
        self.base = Path(self._tmp)
        self.ws = self.base / "myws"
        (self.ws / "sub").mkdir(parents=True)
        for i in range(5):
            (self.ws / "sub" / f"f{i}.txt").write_text(f"data{i}" * 40)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)


def _wrap_open(bomb_add):
    """tarfile.open giả: with-block cầm WRAPPER để bomb_add bắn nổ."""
    real_open = tarfile.open

    class Boom:
        def __init__(self, tf):
            self._tf = tf

        def add(self, *a, **k):
            bomb_add(self._tf, *a, **k)

        def __getattr__(self, item):
            return getattr(self._tf, item)

        def __enter__(self):
            return self          # with-block phải cầm WRAPPER để bắn nổ

        def __exit__(self, *exc):
            return self._tf.__exit__(*exc)

    def fake_open(name, mode="r", **kw):
        tf = real_open(name, mode, **kw)
        return Boom(tf) if mode == "w:gz" else tf

    return fake_open


class TestArchiveLockCleanup(ArchiveBase):
    """Vụ 1 — .lock phải dọn sạch kể cả BaseException, unlink khi còn giữ khóa."""

    def test_1a_keyboardinterrupt_mid_tar_no_lock_left_and_reraises(self):
        """Ctrl-C giữa chừng: KeyboardInterrupt phải nổi nguyên vẹn (không
        bị biến thành StorageError) và KHÔNG sót file .lock."""
        def bomb(tf, *a, **k):
            raise KeyboardInterrupt()

        dest = self.base / "_archives"
        with unittest.mock.patch.object(sm_mod.tarfile, "open",
                                        _wrap_open(bomb)):
            with self.assertRaises(KeyboardInterrupt):
                StorageManager.archive_workspace(self.ws, out_dir=dest)
        leftovers = sorted(p.name for p in dest.iterdir())
        self.assertEqual(
            leftovers, [],
            f"Ctrl-C giữa chừng phải dọn sạch _archives, còn lại: {leftovers}")

    def test_1b_oserror_lock_unlinked_while_still_holding_flock(self):
        """OSError giữa chừng: unlink .lock phải diễn ra TRƯỚC flock(LOCK_UN)
        — nếu unlock xong mới unlink thì process chờ vừa được grant +
        re-validate trên inode cũ lọt vùng găng chung."""
        calls = {"n": 0}

        def bomb(tf, *a, **k):
            calls["n"] += 1
            if calls["n"] > 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return tf.add(*a, **k)

        events = []
        real_flock = fcntl.flock

        def spy_flock(fd, op):
            if op == fcntl.LOCK_UN:
                events.append("unlock")
            return real_flock(fd, op)

        real_unlink = Path.unlink

        def spy_unlink(path_self, *a, **k):
            if str(path_self).endswith(".lock"):
                events.append("unlink-lock")
            return real_unlink(path_self, *a, **k)

        dest = self.base / "_archives"
        with unittest.mock.patch.object(sm_mod.tarfile, "open",
                                        _wrap_open(bomb)):
            with unittest.mock.patch.object(fcntl, "flock", spy_flock), \
                 unittest.mock.patch.object(Path, "unlink", spy_unlink):
                with self.assertRaises(StorageError):
                    StorageManager.archive_workspace(self.ws, out_dir=dest)

        leftovers = sorted(p.name for p in dest.iterdir())
        self.assertEqual(leftovers, [],
                         f"OSError giữa chừng còn rác: {leftovers}")
        self.assertIn("unlock", events, "phải có lần unlock lockfile")
        self.assertIn("unlink-lock", events, "phải có lần unlink .lock")
        self.assertLess(
            events.index("unlink-lock"), events.index("unlock"),
            f"unlink .lock phải xảy ra TRONG LÚC CÒN GIỮ khóa "
            f"(trước LOCK_UN); thứ tự thực tế: {events}")


# ----------------------------------------------------------------------
# Vụ 2 — sync_workspace: removed_local chỉ khi persist thành công
# ----------------------------------------------------------------------

def build_ws(root):
    repo = WorkspaceRepo(root)
    for cid, name, cat in ((1, "Alpha", "web"), (2, "Beta", "pwn")):
        d = os.path.join(root, cat, name.lower())
        for sub in ("challenge", "solver", "writeup"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        repo.write_metadata(os.path.join(d, "metadata.json"),
                            {"id": cid, "name": name, "category": cat,
                             "points": 100})
    return repo


class FakePlatform:
    platform_type = "generic"

    def __init__(self, challenges):
        self.ctf_info = CTFInfo(title="R6", url="https://x.example.com",
                                platform_type=self.platform_type)
        self.ctf_info.challenges = list(challenges)
        self._challenges = list(challenges)

    def authenticate(self):
        return True

    def fetch_challenges(self):
        return list(self._challenges)

    def fetch_solve_attribution(self, ids):
        return {i: None for i in ids}


class TestSyncRemovedLocalOnlyOnPersist(unittest.TestCase):
    def test_2_removed_local_empty_when_mark_removed_raises_oserror(self):
        """mark-removed raise OSError → challenge KHÔNG được liệt kê vào
        removed_local (bảng không được báo 'removed' dù chưa persist);
        lỗi vẫn lộ qua write_errors."""
        tmp = tempfile.mkdtemp(prefix="review6_sync_")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        ws = os.path.join(tmp, "ws")
        repo = build_ws(ws)
        # ghost id=999 chỉ tồn tại local
        ghost_dir = os.path.join(ws, "misc", "ghost")
        os.makedirs(ghost_dir, exist_ok=True)
        repo.write_metadata(os.path.join(ghost_dir, "metadata.json"),
                            {"id": 999, "name": "Ghost", "category": "misc"})

        def boom(repo_, mp, reraise_oserror=False):
            raise OSError(errno.ENOSPC, "No space left on device")

        plat = FakePlatform([make_chall(1, "Alpha", "web"),
                             make_chall(2, "Beta", "pwn")])
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with unittest.mock.patch.object(PullService,
                                        "_mark_removed_from_server", boom):
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                res = PullService.sync_workspace(repo, plat)

        self.assertEqual(
            res["removed_local"], [],
            "mark-removed raise OSError mà removed_local vẫn liệt kê ghost "
            f"— bảng báo removed dù chưa persist: {res['removed_local']}")
        self.assertTrue(
            any("mark-removed" in e for e in res["write_errors"]),
            f"lỗi ghi phải lộ qua write_errors: {res['write_errors']}")


if __name__ == "__main__":
    unittest.main()
