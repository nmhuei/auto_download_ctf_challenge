"""HUNTER CYCLE 8 — vùng chưa hunt: tag/note CLI, storage archive flow,
workspace_repo flock đa tiến trình, pull_service download paths, fileio
atomic_write gián đoạn.

Quy ước: test PASS với hành vi đúng = documentation; FAIL = bug thật
(ghi chú BUG trong docstring từng case). Không đụng production code,
không đụng test_hunter_c7.py. Mock toàn bộ network.
"""
import contextlib
import errno
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent

from ctf_downloader.storage.fileio import (
    atomic_write_text,
    locked_update_json,
)
from ctf_downloader.storage.workspace_repo import WorkspaceRepo
from ctf_downloader.services.status_service import StatusService
from ctf_downloader.services import storage_manager as sm_mod
from ctf_downloader.services.storage_manager import StorageManager, StorageError
from ctf_downloader.downloaders.http_downloader import HttpDownloader


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_workspace(root: Path, chals=None) -> Path:
    """Workspace tối thiểu: challenges.json + metadata.json per challenge."""
    root.mkdir(parents=True, exist_ok=True)
    if chals is None:
        chals = [
            {"id": 1, "name": "Alpha Web", "category": "Web", "points": 100},
            {"id": 2, "name": "Alpha Pwn", "category": "Pwn", "points": 200},
            {"id": 3, "name": "Beta Crypto", "category": "Crypto", "points": 300},
        ]
    (root / "challenges.json").write_text(json.dumps(
        {"ctf_info": {"title": "C8CTF"}, "challenges": chals}), encoding="utf-8")
    for c in chals:
        slug = f"{c['category']}/{c['name'].lower().replace(' ', '_')}"
        d = root / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "metadata.json").write_text(json.dumps(c), encoding="utf-8")
        (d / "challenge").mkdir(exist_ok=True)
    return root


def run_cli(*argv):
    """Chạy cli.main() in-process với sys.argv giả; trả (exit_code, output)."""
    from ctf_downloader.cli import main
    old_argv = sys.argv
    buf_out, buf_err = io.StringIO(), io.StringIO()
    sys.argv = ["ctf"] + list(argv)
    code = 0
    try:
        with contextlib.redirect_stdout(buf_out), \
             contextlib.redirect_stderr(buf_err):
            main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (1 if e.code else 0)
    finally:
        sys.argv = old_argv
    return code, buf_out.getvalue() + buf_err.getvalue()


def status_of(ws: Path, slug: str) -> dict:
    return WorkspaceRepo(ws).read_status(ws / slug / "metadata.json")


class _FakeResp:
    """Response giả cho HttpDownloader: stream raise tuỳ ý."""
    def __init__(self, chunks=None, boom=None, status=200, headers=None):
        self._chunks = chunks or []
        self._boom = boom
        self.status_code = status
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True

    def iter_content(self, chunk_size=65536):
        for c in self._chunks:
            yield c
        if self._boom is not None:
            raise self._boom

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp

    def get(self, *a, **kw):
        return self._resp

    def head(self, *a, **kw):
        raise AssertionError("HEAD không được gọi trong path này")


# ---------------------------------------------------------------------------
# CASE 1 — `ctf tag`: biên validate + exit code + message
# ---------------------------------------------------------------------------

class TagCliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c8_tag_")
        self.ws = make_workspace(Path(self._tmp) / "ws")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_tag_over_24_chars_rejected_exit1(self):
        code, out = run_cli("tag", "Alpha Web", "a" * 25, "-w", str(self.ws))
        self.assertEqual(code, 1, f"exit={code} out={out}")
        self.assertIn("Invalid tag", out)
        self.assertEqual(status_of(self.ws, "Web/alpha_web")["labels"], [])

    def test_tag_special_chars_rejected_uppercase_normalized(self):
        # "Hard!" và "web sec" vi phạm [a-z0-9-]; "Pwn" hợp lệ sau lowercase
        code, out = run_cli("tag", "3", "Hard!", "Pwn", "web sec", "-w", str(self.ws))
        self.assertEqual(code, 1)
        self.assertIn("Invalid tag", out)
        st = status_of(self.ws, "Crypto/beta_crypto")
        # All-or-nothing: batch chứa invalid → KHÔNG ghi nửa vời
        self.assertEqual(st["labels"], [],
                         f"BUG? partial write khi batch có invalid: {st['labels']}")

    def test_tag_valid_lowercase_ok(self):
        code, out = run_cli("tag", "Alpha Web", "Ssti", "xss", "-w", str(self.ws))
        self.assertEqual(code, 0, out)
        self.assertEqual(status_of(self.ws, "Web/alpha_web")["labels"],
                         ["ssti", "xss"])

    def test_tag_remove_nonexistent_is_idempotent_success(self):
        # DOC: remove tag không tồn tại → thành công êm (exit 0), labels giữ nguyên
        run_cli("tag", "Alpha Web", "ssti", "-w", str(self.ws))
        code, out = run_cli("tag", "Alpha Web", "ghost-tag", "-r", "-w", str(self.ws))
        self.assertEqual(code, 0, out)
        self.assertEqual(status_of(self.ws, "Web/alpha_web")["labels"], ["ssti"])

    def test_tag_remove_on_challenge_without_labels(self):
        code, out = run_cli("tag", "2", "anything", "-r", "-w", str(self.ws))
        self.assertEqual(code, 0, out)

    def test_tag_nonexistent_challenge_exit1(self):
        code, out = run_cli("tag", "Nope Challenge", "t1", "-w", str(self.ws))
        self.assertEqual(code, 1, out)
        self.assertIn("not found", out.lower())

    def test_tag_empty_after_strip_usage_error(self):
        # argparse nargs='+' bắt buộc >=1; chuỗi rỗng bị lọc -> usage error exit 1
        code, out = run_cli("tag", "Alpha Web", "", "-w", str(self.ws))
        self.assertEqual(code, 1, out)


# ---------------------------------------------------------------------------
# CASE 2 — `ctf note`: rỗng, --remove đồng thời, multi-line, ambiguous
# ---------------------------------------------------------------------------

class NoteCliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c8_note_")
        self.ws = make_workspace(Path(self._tmp) / "ws")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_note_remove_wins_over_content(self):
        repo = WorkspaceRepo(self.ws)
        mp = self.ws / "Web/alpha_web/metadata.json"
        repo.update_status(mp, lambda st: {**st, "notes": "old"})
        # content + --remove đồng thời: remove phải thắng
        code, out = run_cli("note", "Alpha Web", "new content", "--remove",
                            "-w", str(self.ws))
        self.assertEqual(code, 0, out)
        self.assertEqual(status_of(self.ws, "Web/alpha_web")["notes"], "")

    def test_note_empty_no_stdin_exit1(self):
        code, out = run_cli("note", "Alpha Web", "-w", str(self.ws))
        self.assertEqual(code, 1, out)
        self.assertTrue("empty" in out.lower(), out)

    def test_note_prompt_eof_empty_exit1(self):
        # content bỏ trống → prompt; stdin EOF ngay → note rỗng → exit 1
        code, out = run_cli("note", "Alpha Web", "-w", str(self.ws))
        self.assertEqual(code, 1, out)

    def test_note_multiline_via_argv_quoting(self):
        # argv chứa newline thật (shell quoting tương đương) — lưu NGUYÊN VĂN
        payload = "line1\nline2 thử SSTI {{7*7}}\nline3"
        code, out = run_cli("note", "Alpha Web", payload, "-w", str(self.ws))
        self.assertEqual(code, 0, out)
        self.assertEqual(status_of(self.ws, "Web/alpha_web")["notes"], payload)

    def test_note_prompt_multiline_two_enters_end(self):
        """BUG-C8-4 (test này FAIL = bug thật): set_note KHÔNG truyền nội dung
        prompt vào mutator — ``_mut`` đóng trên tham số ``text`` gốc (None)
        thay vì ``content`` lấy từ prompt (status_service.py:342-344 vs
        :351-357). User gõ note multi-line qua prompt -> lưu RỖNG mà vẫn báo
        "Note saved". Fix 1 dòng: ``st["notes"] = "" if remove else content``.
        """
        repo = WorkspaceRepo(self.ws)
        mp = self.ws / "Web/alpha_web/metadata.json"
        stdin = io.StringIO("dòng A\ndòng B\n\n")
        with mock.patch("sys.stdin", new=stdin), \
             contextlib.redirect_stdout(io.StringIO()):
            ok = StatusService.set_note(repo, "Alpha Web", text=None)
        self.assertTrue(ok)
        self.assertEqual(status_of(self.ws, "Web/alpha_web")["notes"],
                         "dòng A\ndòng B",
                         "BUG-C8-4: note nhập qua prompt bị mất, lưu rỗng")

    def test_note_ambiguous_substring_lists_candidates_exit1(self):
        code, out = run_cli("note", "alpha", "x", "-w", str(self.ws))
        self.assertEqual(code, 1, out)
        # liệt kê candidate, không chọn âm thầm
        self.assertIn("Multiple challenges matched", out)
        self.assertNotEqual(status_of(self.ws, "Web/alpha_web")["notes"], "x")
        self.assertNotEqual(status_of(self.ws, "Pwn/alpha_pwn")["notes"], "x")

    def test_note_nonexistent_challenge_exit1(self):
        code, out = run_cli("note", "ghost", "x", "-w", str(self.ws))
        self.assertEqual(code, 1, out)
        self.assertIn("not found", out.lower())


# ---------------------------------------------------------------------------
# CASE 3 — storage archive flow: git fail / double archive / path lạ / ENOSPC
# ---------------------------------------------------------------------------

class StorageArchiveCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c8_arc_")
        self.base = Path(self._tmp)
        self.ws = self.base / "My CTF Trận cuối 2026"
        make_workspace(self.ws)
        (self.ws / "solver").mkdir()
        (self.ws / "solver" / "sol.py").write_text("print(1)", encoding="utf-8")
        self.env = dict(os.environ,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_archive_basic_unicode_space_path(self):
        res = StorageManager.archive_workspace(self.ws)
        p = Path(res["archive_path"])
        self.assertTrue(p.exists())
        self.assertEqual(p.name, "My CTF Trận cuối 2026_%s.tar.gz"
                         % time.strftime("%Y%m%d"))
        with tarfile.open(p) as tf:
            names = set(tf.getnames())
        self.assertIn("solver/sol.py", names)
        self.assertIn("challenges.json", names)

    def test_archive_exclude_defaults(self):
        (self.ws / "__pycache__").mkdir(exist_ok=True)
        (self.ws / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        junk = self.ws / "junk.part"
        junk.write_bytes(b"x")
        res = StorageManager.archive_workspace(self.ws)
        with tarfile.open(res["archive_path"]) as tf:
            names = "\n".join(tf.getnames())
        self.assertNotIn("__pycache__", names)
        self.assertNotIn("junk.part", names)

    def test_archive_twice_same_day_same_file(self):
        r1 = StorageManager.archive_workspace(self.ws)
        r2 = StorageManager.archive_workspace(self.ws)
        self.assertEqual(r1["archive_path"], r2["archive_path"])
        self.assertTrue(Path(r2["archive_path"]).exists())

    def test_archive_git_add_fails_raises_and_keeps_archive(self):
        """git add fail → StorageError. Archive ĐÃ viết trước git nên còn lại
        (thiết kế chấp nhận được). Kiểm tra không có gì ghi bẩn vào workspace."""
        real_run = subprocess.run

        def fake_run(args, **kw):
            if args[:2] == ["git", "add"]:
                return subprocess.CompletedProcess(args, 1, stdout="",
                                                   stderr="mock add boom")
            return real_run(args, **kw)

        with mock.patch.object(sm_mod.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(StorageError):
                StorageManager.archive_workspace(self.ws, git_remote="https://x/y.git")
        arc_dir = self.ws.parent / "_archives"
        archives = list(arc_dir.glob("*.tar.gz"))
        self.assertEqual(len(archives), 1, "archive phải còn nguyên sau git fail")
        # workspace gốc không bị init .git nhầm
        self.assertFalse((self.ws / ".git").exists())

    def test_archive_git_commit_fail_tolerates_nothing_to_commit_only(self):
        real_run = subprocess.run

        def fake_run(args, **kw):
            if args[:1] == ["git"] and args[1] == "commit":
                return subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="fatal: khác nothing to commit")
            return real_run(args, **kw)

        with mock.patch.object(sm_mod.subprocess, "run", side_effect=fake_run):
            with self.assertRaises(StorageError):
                StorageManager.archive_workspace(self.ws, git_remote="https://x/y.git")

    def test_archive_enospc_mid_tar_leaves_partial_file(self):
        """BUG-C8-1: ENOSPC giữa chừng khi đóng gói phải DỌN .tar.gz nửa chừng
        trong _archives/ và bọc OSError thành StorageError. Repro thật: open
        tar.gz THẬT (file được tạo trên đĩa ngay khi open), entry đầu ghi được
        rồi add kế nổ ENOSPC — trước fix để lại file rác nửa chừng."""
        real_open = tarfile.open

        def exploding_open(*a, **k):
            tf = real_open(*a, **k)

            class BoomWrap:
                def __enter__(self):
                    tf.__enter__()
                    return self

                def __exit__(self, *exc):
                    return tf.__exit__(*exc)

                def add(self, *a, **kw):
                    # Ghi THẬT một entry để archive nửa chừng nằm trên đĩa
                    tf.add(*a, **kw)
                    raise OSError(errno.ENOSPC, "No space left on device")

            return BoomWrap()

        with mock.patch.object(sm_mod.tarfile, "open",
                               side_effect=exploding_open):
            with self.assertRaises(StorageError) as ctx:
                StorageManager.archive_workspace(self.ws)
        self.assertIn("đóng gói", str(ctx.exception))
        leftovers = list((self.ws.parent / "_archives").glob("*.tar.gz"))
        self.assertEqual(
            leftovers, [],
            "BUG-C8-1: ENOSPC giữa chừng để lại tar.gz nửa chừng không dọn")

    def test_delete_workspace_trash_rename_roundtrip(self):
        target = StorageManager.delete_workspace(self.ws)
        self.assertTrue(target.startswith(str(self.base / "_archives")))
        self.assertFalse(self.ws.exists())
        self.assertTrue(Path(target).exists())

    def test_archive_missing_ws_raises_storageerror(self):
        with self.assertRaises(StorageError):
            StorageManager.archive_workspace(self.base / "không_tồn_tại")


# ---------------------------------------------------------------------------
# CASE 4 — workspace_repo flock ĐA TIẾN TRÌNH (subprocess thật, stress)
# ---------------------------------------------------------------------------

_FLOCK_WORKER = r'''
import sys
sys.path.insert(0, sys.argv[1])
from ctf_downloader.storage.workspace_repo import WorkspaceRepo

root, mode, rounds = sys.argv[2], sys.argv[3], int(sys.argv[4])
repo = WorkspaceRepo(root)
if mode == "counter":
    for _ in range(rounds):
        repo.mutate_challenges(lambda d: {**d, "count": (d.get("count") or 0) + 1})
elif mode == "meta":
    # ĐÍCH CHÍNH XÁC truyền vào (tránh os.walk thứ tự khác nhau giữa process)
    meta_path = repo.root / sys.argv[5]
    key = sys.argv[6]
    for i in range(rounds):
        repo.update_metadata(meta_path, lambda m: {**m, key: i})
print("OK")
'''


class FlockMultiprocessCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c8_flock_")
        self.ws = make_workspace(Path(self._tmp) / "ws")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _spawn(self, mode, rounds, key="", rel_meta=""):
        env = dict(os.environ)
        return subprocess.Popen(
            [sys.executable, "-c", _FLOCK_WORKER, str(REPO_ROOT),
             str(self.ws), mode, str(rounds)]
            + ([rel_meta, key] if mode == "meta" else []),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)

    def test_stress_counter_4proc_x15_no_lost_update(self):
        procs = [self._spawn("counter", 15) for _ in range(4)]
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0,
                             f"worker crash:\n{out}\n{err}")
        data = WorkspaceRepo(self.ws).read_challenges()
        count = data.get("count")
        self.assertEqual(count, 60,
                         f"LOST UPDATE! count={count}, kỳ vọng 60 "
                         f"(4 tiến trình x 15 lần cộng)")

    def test_stress_meta_2proc_distinct_keys_both_survive(self):
        rel = "Web/alpha_web/metadata.json"
        pa = self._spawn("meta", 10, "key_a", rel)
        pb = self._spawn("meta", 10, "key_b", rel)
        pc = self._spawn("counter", 10)
        for p in (pa, pb, pc):
            out, err = p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0, f"{out}\n{err}")
        repo = WorkspaceRepo(self.ws)
        mp = self.ws / rel
        meta = repo.read_metadata(mp)
        self.assertEqual(meta.get("key_a"), 9,
                         f"LOST UPDATE key_a: {meta.get('key_a')}")
        self.assertEqual(meta.get("key_b"), 9,
                         f"LOST UPDATE key_b: {meta.get('key_b')}")
        # status block của challenge không bị phá trong lúc tranh chấp
        st = repo.read_status(mp)
        self.assertIn("solve", st)

    def test_no_lock_or_tmp_residue_after_stress(self):
        procs = [self._spawn("counter", 8) for _ in range(3)]
        for p in procs:
            p.communicate(timeout=180)
            self.assertEqual(p.returncode, 0)
        locks = [str(p.relative_to(self.ws)) for p in self.ws.rglob("*.lock")]
        tmps = [str(p.relative_to(self.ws)) for p in self.ws.rglob("*.tmp")]
        self.assertEqual(locks + tmps, [],
                         f"SÓT file khoá/tmp gây kẹt lần sau: {locks + tmps}")
        # ghi tiếp sau stress vẫn thông thoáng (không kẹt lock mồ côi)
        t0 = time.time()
        WorkspaceRepo(self.ws).mutate_challenges(lambda d: {**d, "post": True})
        self.assertLess(time.time() - t0, 10, "ghi sau stress bị treo?")


# ---------------------------------------------------------------------------
# CASE 5 — pull_service download paths: symlink loop + ENOSPC cleanup
# ---------------------------------------------------------------------------

class PullServicePathCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c8_pull_")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_symlink_loop_output_dir_returns_ok_false(self):
        base = Path(self._tmp) / "ws"
        base.mkdir(parents=True)
        loop = base / "loop"
        loop.symlink_to(loop)  # symlink trỏ vào chính nó
        cfg = SimpleNamespace(
            url="https://c8.example.com", cookie=None, token=None,
            custom_headers={}, timeout=5, output_dir=str(loop),
            threads=1, categories=None, exclude_categories=None,
            force_redownload=False, size_limit_bytes=0,
            create_solve_template=False, download_third_party=False,
            refresh_meta=False, validate=lambda: None,
        )
        fake_platform = SimpleNamespace(authenticate=lambda: True,
                                        fetch_challenges=lambda: [
                                            SimpleNamespace(id=1, name="A",
                                                            category="Web")])
        from ctf_downloader.services import pull_service as ps_mod
        with mock.patch.object(ps_mod.PlatformDetector, "detect_platform",
                               return_value=fake_platform):
            res = ps_mod.PullService.run(cfg)
        self.assertFalse(res["ok"],
                         f"BUG? symlink loop vẫn báo ok=True: {res}")
        self.assertEqual(res["total_files"], 0)

    def test_download_file_enospc_cleans_part_tmp(self):
        dest = Path(self._tmp) / "dl"
        resp = _FakeResp(chunks=[b"x" * 100],
                         boom=OSError(errno.ENOSPC, "No space left"))
        got = HttpDownloader.download_file(
            "https://c8.example.com/f.bin", str(dest), _FakeSession(resp),
            preferred_filename="f.bin")
        self.assertIsNone(got, "ENOSPC phải trả None (thất bại sạch)")
        leftovers = sorted(p.name for p in dest.glob("*")
                           if p.name.endswith((".part", ".tmp")))
        self.assertEqual(leftovers, [],
                         f"BUG-C8-2? sót file tạm sau ENOSPC: {leftovers}")
        self.assertFalse((dest / "f.bin").exists(),
                         "file đích không được phép nửa chừng")

    def test_save_response_stream_enospc_cleans_tmp(self):
        dest = Path(self._tmp) / "dl2"
        dest.mkdir(parents=True)
        resp = _FakeResp(chunks=[b"x" * 50],
                         boom=OSError(errno.ENOSPC, "No space left"))
        got = HttpDownloader.save_response_stream(
            resp, str(dest), "g.bin")
        self.assertIsNone(got)
        leftovers = [p.name for p in dest.glob("*")
                     if p.name.endswith((".part", ".tmp"))]
        self.assertEqual(leftovers, [], f"sót tmp: {leftovers}")

    def test_download_large_skip_cleans_part(self):
        dest = Path(self._tmp) / "dl3"
        resp = _FakeResp(chunks=[b"x" * 100], status=200, headers={})
        got = None
        from ctf_downloader.downloaders.http_downloader import LargeFileSkipped
        try:
            got = HttpDownloader.download_file(
                "https://c8.example.com/big.bin", str(dest),
                _FakeSession(resp), preferred_filename="big.bin",
                max_size=10)
        except LargeFileSkipped:
            pass
        self.assertIsNone(got)
        leftovers = [p.name for p in dest.glob("*")
                     if p.name.endswith((".part", ".tmp"))]
        self.assertEqual(leftovers, [], f"sót sau large-skip: {leftovers}")


# ---------------------------------------------------------------------------
# CASE 6 — fileio atomic_write gián đoạn + .bak phục hồi
# ---------------------------------------------------------------------------

class FileioAtomicCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c8_io_")
        self.d = Path(self._tmp)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_atomic_write_replace_interrupt_original_intact(self):
        """BUG-C8-3 (test này FAIL = bug thật): atomic_write_text lỗi giữa
        chừng (vd ENOSPC ở os.replace) để lại ``<name>.tmp`` trên đĩa —
        file gốc nguyên vẹn (điểm cộng) nhưng không dọn tmp, trong khi
        locked_update_json có dọn (fileio.py:30-34, không try/except).
        Fix 1 dòng: bọc write+replace trong try/except unlink tmp."""
        p = self.d / "state.json"
        atomic_write_text(p, '{"v":1}')
        real_replace = os.replace
        with mock.patch.object(sm_mod.os, "replace",
                               side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                atomic_write_text(p, '{"v":2}')
        # GỐC phải nguyên vẹn
        self.assertEqual(p.read_text(encoding="utf-8"), '{"v":1}')
        tmp_leftover = list(self.d.glob("state.json.tmp"))
        if tmp_leftover:
            self.fail("BUG-C8-3: atomic_write_text lỗi giữa chừng để lại "
                      f".tmp (locked_update_json thì có dọn): {tmp_leftover}")

    def test_locked_update_replace_interrupt_no_tmp_lock_kept_then_recovers(self):
        p = self.d / "meta.json"
        p.write_text('{"v":1}', encoding="utf-8")
        with mock.patch.object(sm_mod.os, "replace",
                               side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                locked_update_json(p, lambda d: {**d, "v": 2})
        # gốc nguyên vẹn, không tmp sót
        self.assertEqual(json.loads(p.read_text()), {"v": 1})
        self.assertEqual(list(self.d.glob("*.tmp")), [],
                         "locked_update_json phải tự dọn tmp khi fail")
        lock = self.d / "meta.json.lock"
        # docstring: ghi THẤT BẠT -> lockfile được GIỮ lại (chấp nhận được)
        # nhưng lần ghi SAU phải thành công ngay (không kẹt)
        t0 = time.time()
        out = locked_update_json(p, lambda d: {**d, "v": 3})
        self.assertLess(time.time() - t0, 10, "ghi sau thất bại bị kẹt lock?")
        self.assertEqual(out["v"], 3)
        self.assertFalse(lock.exists(), "ghi thành công phải unlink lockfile")

    def test_corrupt_json_backed_up_to_bak_before_overwrite(self):
        p = self.d / "challenges.json"
        corrupted = '{"ctf_info": broken...'
        p.write_text(corrupted, encoding="utf-8")
        locked_update_json(p, lambda d: {**d, "repaired": True})
        bak = self.d / "challenges.json.bak"
        self.assertTrue(bak.exists(), ".bak phải chứa nội dung hỏng trước khi đè")
        self.assertEqual(bak.read_text(encoding="utf-8"), corrupted)
        data = json.loads(p.read_text())
        self.assertTrue(data.get("repaired"))

    def test_mutator_raising_keeps_file_and_cleans(self):
        p = self.d / "x.json"
        p.write_text('{"keep": true}', encoding="utf-8")

        def boom(_d):
            raise RuntimeError("mutator nổ")

        with self.assertRaises(RuntimeError):
            locked_update_json(p, boom)
        self.assertEqual(json.loads(p.read_text()), {"keep": True})
        self.assertEqual([q.name for q in self.d.glob("x.json.*")],
                         ["x.json.lock"])  # giữ lock theo docstring, tmp phải dọn

    def test_symlink_target_write_goes_through_resolve(self):
        real = self.d / "real.json"
        atomic_write_text(real, '{"a":1}')
        link = self.d / "link.json"
        link.symlink_to(real)
        locked_update_json(link, lambda d: {**(d or {}), "b": 2})
        self.assertTrue(link.is_symlink(),
                        "os.replace lên path symlink sẽ thay link bằng file thường")
        self.assertEqual(json.loads(real.read_text())["b"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
