"""HUNTER CYCLE 9 — pull/download paths + data integrity.

Toàn bộ HTTP mock, tmp_path local, KHÔNG mạng thật. KHÔNG sửa production code.
Vùng: consent gate >1GB, zip/extract surface, filename sanitize, incremental
update state preservation, thread race trên cùng file đích, cross-check repo-only.

Quy ước hunter: assertion mã hoá HÀNH VI ĐÚNG -> PASS = documentation,
FAIL = bug thật (được liệt kê trong report kèm id).

Chạy: python3 -m pytest test_hunter_c9.py -q
"""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import requests

from ctf_downloader.config import DownloaderConfig
from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.downloaders.http_downloader import HttpDownloader, LargeFileSkipped
from ctf_downloader.downloaders.manager import DownloadManager
from ctf_downloader.generator.summary_generator import SummaryGenerator
from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
from ctf_downloader.services import pull_service
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo
from ctf_downloader.utils.sanitize import (
    sanitize_filename,
    sanitize_folder_name,
)

GB = 1073741824


# --------------------------------------------------------------------------- #
# HTTP fakes
# --------------------------------------------------------------------------- #
class FakeStreamResponse:
    def __init__(self, chunks, status_code=200, headers=None):
        self._chunks = list(chunks)
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.closed = False

    def iter_content(self, chunk_size=65536):
        for ch in self._chunks:
            yield ch

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class DroppingStreamResponse(FakeStreamResponse):
    """Stream raise RequestException sau khi yield ``raise_after`` chunk."""

    def __init__(self, chunks, raise_after, **kw):
        super().__init__(chunks, **kw)
        self._raise_after = raise_after

    def iter_content(self, chunk_size=65536):
        for i, ch in enumerate(self._chunks):
            if i >= self._raise_after:
                raise requests.ConnectionError("mid-stream drop")
            yield ch


class FakeSession:
    def __init__(self, head_headers=None, get_resp=None,
                 head_exc=None, get_exc=None):
        self.head_headers = dict(head_headers or {})
        self.get_resp = get_resp
        self.head_exc = head_exc
        self.get_exc = get_exc
        self.get_calls = 0
        self.last_get_headers = None

    def head(self, url, timeout=None, allow_redirects=False):
        if self.head_exc is not None:
            raise self.head_exc
        return FakeStreamResponse([], status_code=200,
                                  headers=self.head_headers)

    def get(self, url, stream=False, timeout=None,
            allow_redirects=True, headers=None):
        self.get_calls += 1
        self.last_get_headers = headers
        if self.get_exc is not None and self.get_calls > 1:
            # lần đầu trả resp (để có thể drop giữa chừng), các lần sau lỗi kết nối
            if not isinstance(self.get_exc, tuple) or True:
                pass
        if self.get_exc is not None:
            if self.get_calls == 1 and self.get_resp is not None:
                return self.get_resp
            raise self.get_exc
        return self.get_resp


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class FakeNotTTY(io.StringIO):
    def isatty(self):
        return False


# =========================================================================== #
# CASE 1 — Consent gate >1GB: boundary, tty/non-tty, .part cleanup
# =========================================================================== #
class TestConsentGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c9_consent_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _manager(self, limit=1024):
        return DownloadManager(session=FakeSession(), size_limit_bytes=limit)

    def test_01_boundary_exact_limit_allowed_without_asking(self):
        """Đúng bằng ngưỡng (1024) phải ĐƯỢC tải, không hỏi consent."""
        m = self._manager(limit=1024)
        with patch("builtins.input", side_effect=AssertionError("không được hỏi")):
            self.assertTrue(m._confirm_large_download("https://x/f.bin", 1024))
        # boundary qua đường download_url đầy đủ
        body = b"A" * 1024
        s = FakeSession(head_headers={"Content-Length": "1024"},
                        get_resp=FakeStreamResponse([body[:512], body[512:]],
                                                    headers={"Content-Length": "1024"}))
        m2 = DownloadManager(session=s, size_limit_bytes=1024)
        ok, path, msg = m2.download_url("https://x/f.bin", self.tmp,
                                        preferred_name="f.bin")
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.isfile(path))

    def test_02_boundary_plus_one_tty_decline(self):
        """Ngưỡng +1 byte phải được hỏi; trả lời 'n' -> skip, không ghi file."""
        s = FakeSession(head_headers={"Content-Length": "1025"},
                        get_resp=FakeStreamResponse([b"A" * 1025]))
        m = DownloadManager(session=s, size_limit_bytes=1024)
        asked = []

        def fake_input(prompt):
            asked.append(prompt)
            return "n"

        with patch("sys.stdin", FakeTTY()), \
             patch("builtins.input", side_effect=fake_input):
            ok, path, msg = m.download_url("https://x/big.bin", self.tmp,
                                           preferred_name="big.bin")
        self.assertFalse(ok)
        self.assertIn("skipped_large_file", msg)
        self.assertEqual(len(asked), 1, "phải hỏi đúng 1 lần")
        self.assertEqual(os.listdir(self.tmp), [], "không được ghi byte nào")

    def test_03_tty_accept_downloads_fully(self):
        """BUG C9-05: user trả lời 'y' đồng ý tải file vượt ngưỡng, nhưng
        manager vẫn truyền max_size=size_limit vào download_file -> hard gate
        raise LargeFileSkipped NGAY SAU khi user đã đồng ý -> consent accept
        VÔ ÍCH, file vẫn bị bỏ."""
        s = FakeSession(head_headers={"Content-Length": "2048"},
                        get_resp=FakeStreamResponse([b"B" * 1024, b"B" * 1024],
                                                    headers={"Content-Length": "2048"}))
        m = DownloadManager(session=s, size_limit_bytes=1024)
        with patch("sys.stdin", FakeTTY()), \
             patch("builtins.input", return_value="y"):
            ok, path, msg = m.download_url("https://x/big.bin", self.tmp,
                                           preferred_name="big.bin")
        self.assertTrue(ok,
                        "C9-05: user đã đồng ý ('y') nhưng file vẫn bị từ chối "
                        f"(hard gate đè lên consent): {msg}")
        self.assertTrue(path and os.path.isfile(path))
        with open(path, "rb") as f:
            self.assertEqual(f.read(), b"B" * 2048)

    def test_04_non_tty_auto_rejects_never_hangs(self):
        """Pipe/stdin không tty: tự từ chối kèm cảnh báo, KHÔNG gọi input."""
        s = FakeSession(head_headers={"Content-Length": "5000"},
                        get_resp=FakeStreamResponse([b"A" * 5000]))
        m = DownloadManager(session=s, size_limit_bytes=1024)
        with patch("sys.stdin", FakeNotTTY()), \
             patch("builtins.input",
                   side_effect=AssertionError("non-tty không được prompt")):
            ok, path, msg = m.download_url("https://x/big.bin", self.tmp,
                                           preferred_name="big.bin")
        self.assertFalse(ok)
        self.assertIn("skipped_large_file", msg)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_05_unknown_size_midstream_abort_cleans_part(self):
        """Probe fail (unknown size) -> không hỏi consent nhưng stream vượt
        ngưỡng giữa chừng phải ngắt sớm + DỌN .part."""
        s = FakeSession(head_exc=requests.ConnectionError("head blocked"),
                        get_resp=DroppingStreamResponse(
                            [b"C" * 512] * 8, raise_after=3))
        m = DownloadManager(session=s, size_limit_bytes=1024)
        ok, path, msg = m.download_url("https://x/unk.bin", self.tmp,
                                       preferred_name="unk.bin")
        self.assertFalse(ok)
        self.assertIn("skipped_large_file", msg)
        leftovers = [f for f in os.listdir(self.tmp) if f.endswith(".part")]
        self.assertEqual(leftovers, [],
                         f".part phải được dọn khi abort giữa chừng, còn: {leftovers}")

    def test_06_direct_pregate_over_limit_no_byte_written(self):
        """HttpDownloader.download_file gate NGAY TRƯỚC khi ghi: CL > max ->
        raise LargeFileSkipped, dest trống."""
        s = FakeSession(get_resp=FakeStreamResponse([b"D" * 1025],
                                                    headers={"Content-Length": "1025"}))
        with self.assertRaises(LargeFileSkipped):
            HttpDownloader.download_file("https://x/f", self.tmp, s,
                                         preferred_filename="f.bin", max_size=1024)
        self.assertEqual(os.listdir(self.tmp), [])

    def test_07_direct_pregate_exact_limit_ok(self):
        body = b"E" * 1024
        s = FakeSession(get_resp=FakeStreamResponse([body],
                                                    headers={"Content-Length": "1024"}))
        p = HttpDownloader.download_file("https://x/f", self.tmp, s,
                                         preferred_filename="f.bin", max_size=1024)
        self.assertTrue(p and os.path.isfile(p))
        with open(p, "rb") as f:
            self.assertEqual(f.read(), body)

    def test_08_consent_asked_again_for_each_large_file(self):
        """Documentation: consent KHÔNG nhớ lựa chọn — mỗi file lớn đều hỏi lại.
        (per-file consent là hành vi thiết kế; test ghi nhận 2 lần hỏi.)"""
        s = FakeSession(head_headers={"Content-Length": "2000"},
                        get_resp=FakeStreamResponse([b"F" * 2000],
                                                    headers={"Content-Length": "2000"}))
        m = DownloadManager(session=s, size_limit_bytes=1024)
        calls = []

        def fake_input(prompt):
            calls.append(prompt)
            return "y"

        with patch("sys.stdin", FakeTTY()), \
             patch("builtins.input", side_effect=fake_input):
            m.download_url("https://x/a.bin", self.tmp, preferred_name="a.bin")
            m.download_url("https://x/b.bin", self.tmp, preferred_name="b.bin")
        self.assertEqual(len(calls), 2,
                         "mỗi file lớn được hỏi consent riêng (không có memory)")

    def test_09_part_kept_on_exhausted_retries_by_design(self):
        """Mất kết nối lặp lại đến khi hết số lần thử: download trả None nhưng
        .part được GIỮ lại chủ ý để lần chạy SAU resume tiếp (cross-run)."""
        first = DroppingStreamResponse([b"G" * 512] * 8, raise_after=2)
        s = FakeSession(get_exc=requests.ConnectionError("net gone"),
                        get_resp=first)
        m = DownloadManager(session=s, size_limit_bytes=0)  # tắt gate
        ok, path, msg = m.download_url("https://x/r.bin", self.tmp,
                                       preferred_name="r.bin")
        self.assertFalse(ok)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "r.bin.part")),
                        ".part giữ lại để resume lần chạy sau (thiết kế)")
        self.assertFalse(any(f == "r.bin" for f in os.listdir(self.tmp)),
                         "chưa hoàn chỉnh thì chưa được rename thành target")


# =========================================================================== #
# CASE 2 — Zip/extract surface: không có đường extract archive nào
# =========================================================================== #
class TestNoArchiveExtraction(unittest.TestCase):
    def test_10_no_zip_slip_surface_in_download_tree(self):
        """Tool KHÔNG extract zip/tar đã tải (chỉ storage_manager TẠO archive).
        Static scan khẳng định không có extractall/ZipFile-read/tarfile-open-r
        trong vùng download/build -> không có bề mặt zip-slip."""
        root = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "ctf_downloader")
        offenders = []
        for dirpath, _dirs, files in os.walk(root):
            if "__pycache__" in dirpath:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                p = os.path.join(dirpath, fn)
                with open(p, encoding="utf-8") as f:
                    src = f.read()
                # storage_manager CHỈ ĐƯỢC PHÉP TẠO archive (w mode) — cấm extract
                if p.endswith("storage_manager.py"):
                    if "extractall" in src or 'tarfile.open(archive_path, "r"' in src \
                            or 'zipfile.ZipFile(zip_path, "r"' in src:
                        offenders.append(f"{p}: read-mode extract")
                    continue
                for needle in ("extractall", "ZipFile(", "tarfile.open"):
                    if needle in src:
                        offenders.append(f"{p}: {needle}")
        self.assertEqual(offenders, [],
                         "phát hiện code extract archive ngoài storage_manager")

    def test_11_zip_attachment_stored_as_is(self):
        """Zip được tải về NGUYÊN TRẠNG (không auto-extract) — hành vi an toàn."""
        tmp = tempfile.mkdtemp(prefix="c9_zip_")
        try:
            zip_bytes = b"PK\x03\x04not-a-real-zip-but-opaque"
            s = FakeSession(head_headers={"Content-Length": str(len(zip_bytes))},
                            get_resp=FakeStreamResponse([zip_bytes]))
            m = DownloadManager(session=s, size_limit_bytes=0)
            ok, path, msg = m.download_url("https://x/chal.zip", tmp,
                                           preferred_name="chal.zip")
            self.assertTrue(ok, msg)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), zip_bytes, "file phải nguyên vẹn, không extract")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# =========================================================================== #
# CASE 3 — Filename sanitize: control chars, traversal, collision, byte-limit
# =========================================================================== #
class TestSanitize(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c9_san_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_12_control_chars_and_null_byte_removed(self):
        for raw in ("we\x00b\x07log", "a\x1fb", "\x00\x00"):
            out = sanitize_folder_name(raw)
            self.assertNotIn("\x00", out)
            self.assertTrue(all(ord(ch) >= 32 for ch in out), repr(out))
            # tên result phải dùng được làm thư mục thật
            d = os.path.join(self.tmp, out)
            os.makedirs(d, exist_ok=True)

    def test_13_traversal_style_names_stay_single_component(self):
        for raw in ("../../etc/passwd", "/abs/path", "..\\..\\win", ".."):
            out = sanitize_folder_name(raw)
            self.assertNotIn("/", out)
            self.assertNotIn("\\", out)
            self.assertNotEqual(out.strip(), "..")
            self.assertFalse(out.startswith("../"))
            d = os.path.join(self.tmp, out)
            os.makedirs(d, exist_ok=True)   # không escape ra ngoài tmp
            self.assertTrue(os.path.dirname(os.path.realpath(d))
                            == os.path.realpath(self.tmp),
                            f"'{raw}' -> '{out}' escape khỏi dest_dir")

    def test_14_duplicate_names_after_sanitize_collide_and_corrupt_metadata(self):
        """BUG C9-01: hai challenge khác id but tên sanitize trùng
        ('web/login' vs 'web:login' -> 'web_login') chia sẻ MỘT thư mục:
        metadata.json của bài sau GHI ĐÈ bài trước trong khi README vẫn của
        bài trước -> tree mâu thuẫn, mất metadata bài 1."""
        chall_a = Challenge(id=1, name="web/login", category="Web",
                            description="plain desc alpha no links.")
        chall_b = Challenge(id=2, name="web:login", category="Web",
                            description="plain desc beta no links.")
        self.assertEqual(sanitize_folder_name(chall_a.name),
                         sanitize_folder_name(chall_b.name),
                         "tiền đề: hai tên va nhau sau sanitize")
        WorkspaceBuilder.create_challenge_workspace(self.tmp, chall_a, [], [], [])
        WorkspaceBuilder.create_challenge_workspace(self.tmp, chall_b, [], [], [])
        meta = json.load(open(os.path.join(self.tmp, "Web", "web_login",
                                           "metadata.json"), encoding="utf-8"))
        readme = open(os.path.join(self.tmp, "Web", "web_login", "challenge",
                                   "README.md"), encoding="utf-8").read()
        # HÀNH VI ĐÚNG: cả hai challenge phải còn nhận diện riêng biệt
        self.assertEqual(meta.get("id"), 1,
                         "metadata challenge A bị B ghi đè mất "
                         "(C9-01: collision sau sanitize ở download tree)")

    def test_15_long_unicode_name_respects_255byte_fs_limit(self):
        """BUG C9-02: sanitize_folder_name cap theo SỐ KÝ TỰ (80) chứ không
        theo BYTE UTF-8; 80 emoji = 320 byte > NAME_MAX 255 của Linux ->
        os.makedirs OSError, challenge bị rơi khỏi workspace."""
        name = "🔥" * 100
        clean = sanitize_folder_name(name)
        self.assertLessEqual(len(clean.encode("utf-8")), 255,
                             f"tên {len(clean.encode('utf-8'))} byte vượt "
                             f"NAME_MAX 255 -> makedirs sẽ crash (C9-02)")
        # Cập nhật theo convention cycle-6 ("test theo hành vi đúng"): khối
        # assertRaises(OSError) cũ tự mâu thuẫn — tên đã được cắt <=254 byte
        # thì HỢP LỆ và makedirs không bao giờ nổ. Hành vi đúng cần chứng
        # minh end-to-end: đúng tên đó tạo được thư mục THẬT trên đĩa.
        d = os.path.join(self.tmp, clean)
        os.makedirs(d, exist_ok=True)
        self.assertTrue(os.path.isdir(d))


# =========================================================================== #
# CASE 4 — Incremental/update: giữ trạng thái user, missing handling
# =========================================================================== #
class _UpdateCounter(pull_service.DownloadManager):
    download_calls = []

    def download_challenge_files(self, *args, **kwargs):
        files = args[0] if args else kwargs.get("files")
        dest_dir = args[2] if len(args) > 2 else kwargs.get("dest_dir")
        type(self).download_calls.append(files)
        results = []
        for url, name in (files or []):
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, name)
            with open(path, "wb") as f:
                f.write(b"fake-download-bytes")
            results.append({"url": url, "name": name, "saved_path": path,
                            "success": True, "source": "platform_attachment"})
        return results


class FakePlatform:
    platform_type = "generic"

    def __init__(self, challenges, title="C9CTF", url="https://c9.example.com"):
        self.ctf_info = CTFInfo(title=title, url=url,
                                platform_type=self.platform_type)
        self.ctf_info.challenges = list(challenges)
        self._challenges = list(challenges)

    def authenticate(self):
        return True

    def fetch_challenges(self):
        return list(self._challenges)


def make_chall(cid, name, category, points=100, **kw):
    kw.setdefault("description", f"Plain description of {name}. No links.")
    kw.setdefault("files", [])
    return Challenge(id=cid, name=name, category=category, points=points, **kw)


def detect_patch(platform):
    return patch.object(pull_service.PlatformDetector, "detect_platform",
                        return_value=platform)


def dm_patch():
    return patch.object(pull_service, "DownloadManager", _UpdateCounter)


class TestIncrementalState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c9_inc_")
        self.out = os.path.join(self.tmp, "ws")
        _UpdateCounter.download_calls = []
        self.cfg = DownloaderConfig(url="https://c9.example.com",
                                    cookie="s=1", output_dir=self.out, threads=2)
        round1 = FakePlatform([make_chall(1, "Alpha", "Web"),
                               make_chall(2, "Beta", "Pwn")])
        with detect_patch(round1), dm_patch():
            r = PullService.run(self.cfg)
        self.assertTrue(r["ok"])
        self.repo = WorkspaceRepo(self.out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def meta_of(self, cid):
        for mp in self.repo.iter_challenges():
            m = self.repo.read_metadata(mp)
            if str(m.get("id")) == str(cid):
                return mp
        self.fail(f"no metadata id={cid}")

    def test_16_update_preserves_solved_notes_tags(self):
        """--update đổi points/connection: solved/notes/tags của bài cũ PHẢI giữ."""
        mp = self.meta_of(1)
        self.repo.update_status(mp, lambda st: {
            **st, "solve": "solved_by_me", "notes": "secret note",
            "labels": ["heap", "glibc-2.35"]})
        self.repo.update_metadata(mp, lambda m: {**m, "tags": ["uaf"]})
        round2 = FakePlatform([
            make_chall(1, "Alpha", "Web", points=250,
                       connection_info="nc host 1337"),
            make_chall(2, "Beta", "Pwn")])
        cfg = DownloaderConfig(url="https://c9.example.com", cookie="s=1",
                               output_dir=self.out, threads=1,
                               incremental_update=True)
        with detect_patch(round2), dm_patch():
            r = PullService.run_update(cfg)
        self.assertTrue(r["ok"])
        meta = self.repo.read_metadata(mp)
        st = self.repo.read_status(mp)
        self.assertEqual(meta.get("points"), 250, "metadata động phải cập nhật")
        self.assertEqual(st["solve"], "solved_by_me")
        self.assertEqual(st["notes"], "secret note")
        self.assertEqual(st["labels"], ["heap", "glibc-2.35"])
        self.assertEqual(meta.get("tags"), ["uaf"], "tags user/platform cũ giữ nguyên")

    def test_17_missing_from_server_marked_not_deleted(self):
        mp = self.meta_of(1)
        chall_dir = os.path.dirname(mp)
        marker = os.path.join(chall_dir, "challenge", "keepme.txt")
        with open(marker, "w") as f:
            f.write("local work")
        round2 = FakePlatform([make_chall(2, "Beta", "Pwn")])  # Alpha biến mất
        cfg = DownloaderConfig(url="https://c9.example.com", cookie="s=1",
                               output_dir=self.out, threads=1,
                               incremental_update=True)
        with detect_patch(round2), dm_patch():
            r = PullService.run_update(cfg)
        self.assertTrue(r["ok"])
        self.assertEqual(r["missing"], 1)
        self.assertTrue(os.path.isdir(chall_dir), "không được xoá local")
        self.assertTrue(os.path.isfile(marker))
        meta = self.repo.read_metadata(mp)
        self.assertIs(meta.get("removed_from_server"), True)
        self.assertEqual((meta.get("status") or {}).get("removed_from_server"), True)

    def test_18_refresh_meta_redownload_loses_local_instance_state(self):
        """BUG C9-03: --refresh-meta tải lại attachment -> WorkspaceBuilder viết
        lại toàn bộ metadata.json; _restore_user_fields chỉ khôi phục
        (status, submitted_flag) — instance_info DO INSTANCE SERVICE QUẢN TRÊN
        ĐỊA (is_container/active_instance/remaining_time) BIẾT MẤT."""
        mp = self.meta_of(1)
        saved = os.path.join(os.path.dirname(mp), "challenge", "attach.bin")
        os.makedirs(os.path.dirname(saved), exist_ok=True)
        with open(saved, "wb") as f:
            f.write(b"old")
        self.repo.update_metadata(mp, lambda m: {
            **m,
            "downloaded_files": [{"url": "https://c9.example.com/a.bin",
                                  "name": "attach.bin", "saved_path": saved,
                                  "success": True}],
            "instance_info": {"is_container": True,
                              "active_instance": "10.0.0.5:1337",
                              "remaining_time": 900}})
        self.repo.update_status(mp, lambda st: {
            **st, "solve": "solved_by_me", "notes": "keep me"})
        os.remove(saved)   # attachment thiếu trên đĩa

        # Platform KHÔNG biết gì về container local (instance_info rỗng)
        round2 = FakePlatform([make_chall(
            1, "Alpha", "Web",
            files=[("https://c9.example.com/a.bin", "attach.bin")])])
        cfg = DownloaderConfig(url="https://c9.example.com", cookie="s=1",
                               output_dir=self.out, threads=1,
                               refresh_meta=True)
        with detect_patch(round2), dm_patch():
            r = PullService.run_update(cfg)
        self.assertTrue(r["ok"])
        self.assertTrue(os.path.isfile(saved), "attachment phải được tải lại")
        st = self.repo.read_status(mp)
        self.assertEqual(st["solve"], "solved_by_me", "status phải sống sót")
        meta = self.repo.read_metadata(mp)
        inst = meta.get("instance_info") or {}
        self.assertEqual(inst.get("active_instance"), "10.0.0.5:1337",
                         f"C9-03: local instance state bị mất sau refresh-meta "
                         f"redownload, instance_info giờ={inst}")


# =========================================================================== #
# CASE 5 — Thread race: hai worker ghi cùng file đích
# =========================================================================== #
class TestThreadRace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c9_race_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_19_two_threads_same_target_silent_corruption(self):
        """BUG C9-04: hai worker của pool tải 2 attachment TRÙNG TÊN đích
        (chỉ cần hai challenge va nhau sau sanitize — C9-01 — rơi vào cùng
        thư mục challenge/) vào cùng dest_dir. Chuỗi sự cố quan sát được:
        thread sau thấy `.part` của thread trước -> giả resume gửi
        ``Range: bytes=0-``, server trả 200 -> XOÁ .part và mở lại "wb";
        các chunk của thread trước giờ ghi vào inode ĐÃ UNLINK (mất im
        lặng), còn thread sau rename file MÌNH vừa ghi và báo THÀNH CÔNG
        với nội dung của URL KHÁC. Kết quả đúng: file 'success' phải là
        nội dung server mà CHÍNH thread đó đã tải."""
        part_path = os.path.join(self.tmp, "dup.bin.part")
        target_path = os.path.join(self.tmp, "dup.bin")
        e_a_open = threading.Event()
        e_b1 = threading.Event()

        a_full = b"A" * 1024   # 2 chunk x 512 — nội dung server A
        b_full = b"B" * 512    # 1 chunk     — nội dung server B

        class GenResp(FakeStreamResponse):
            def __init__(self, gen):
                super().__init__([])
                self._gen = gen()

            def iter_content(self, chunk_size=65536):
                for ch in self._gen:
                    yield ch

        def gen_a():
            e_a_open.set()          # A đã mở .part (wb)
            yield a_full[:512]      # A1 @0..512 (fd offset A -> 512)
            e_b1.wait(5)
            yield a_full[512:]      # A2 — ghi tiếp ở offset 512 của fd cũ

        def gen_b():
            yield b_full            # B ghi vào .part MỚI sau khi xoá .part cũ
            e_b1.set()
            # giữ fd mở tới khi A rename xong (.part biến mất); chunk rỗng
            # là no-op (download_file skip `if not chunk`) để khoảnh đóng muộn
            deadline = time.time() + 5
            while os.path.exists(part_path) and time.time() < deadline:
                time.sleep(0.005)
            yield b""

        sess_a = FakeSession(get_resp=GenResp(gen_a))
        sess_b = FakeSession(get_resp=GenResp(gen_b))
        m_a = DownloadManager(session=sess_a, size_limit_bytes=0)
        m_b = DownloadManager(session=sess_b, size_limit_bytes=0)

        results = {}

        def run(m, tag, wait_event=None):
            if wait_event is not None:
                wait_event.wait(5)
            results[tag] = m.download_url("https://x/dup.bin", self.tmp,
                                          preferred_name="dup.bin")

        ta = threading.Thread(target=run, args=(m_a, "a"))
        tb = threading.Thread(target=run, args=(m_b, "b", e_a_open))
        ta.start()
        tb.start()
        ta.join(10)
        tb.join(10)

        self.assertTrue(os.path.isfile(target_path), "phải có file hoàn tất")
        with open(target_path, "rb") as f:
            final = f.read()
        ok_a = results.get("a", (None,))[0]
        ok_b = results.get("b", (None,))[0]
        # HÀNH VI ĐÚNG: thread thành công phải có file nguyên vẹn NỘI DUNG CỦA CHÍNH NÓ
        if ok_a:
            self.assertEqual(final, a_full,
                             f"C9-04: A báo success nhưng file không phải dữ liệu "
                             f"của A (len={len(final)}, head={final[:16]!r}) — "
                             f"bị trộn/đè bởi thread khác hoặc mất chunk qua "
                             f"inode unlink; ok_b={ok_b}")
        elif ok_b:
            self.assertEqual(final, b_full,
                             f"C9-04: B báo success nhưng file không phải dữ "
                             f"liệu của B (len={len(final)})")
        else:
            self.fail("C9-04: cả hai thread thất bại mà vẫn để lại file trên đĩa")

    def test_20_control_distinct_names_both_intact(self):
        """Control: hai thread, tên file KHÁC nhau -> cả hai nguyên vẹn."""
        bodies = {"one.bin": b"1" * 4096, "two.bin": b"2" * 4096}
        mgrs = {}
        for name, body in bodies.items():
            s = FakeSession(get_resp=FakeStreamResponse(
                [body[:2048], body[2048:]]))
            mgrs[name] = DownloadManager(session=s, size_limit_bytes=0)
        results = {}

        def run(name):
            m = mgrs[name]
            results[name] = m.download_url(f"https://x/{name}", self.tmp,
                                           preferred_name=name)

        ts = [threading.Thread(target=run, args=(n,)) for n in bodies]
        for t in ts:
            t.start()
        for t in ts:
            t.join(10)
        for name, body in bodies.items():
            ok, path, _msg = results[name]
            self.assertTrue(ok, name)
            with open(path, "rb") as f:
                self.assertEqual(f.read(), body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
