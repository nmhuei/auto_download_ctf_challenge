"""hunt-c19 — regression tests cho findings trên pipeline tải xuống.

Chạy: python3 -m pytest test_hunter_c19.py -q
Toàn bộ HTTP được mock — KHÔNG gọi mạng thật.

Phủ các finding theo mức:
  - C19-H1 [HIGH]: đích tải tính bằng HÀM DUY NHẤT (resolve_challenge_dir,
    có guard owner/-id) dùng chung _full_process lẫn WorkspaceBuilder —
    hai challenge sanitize trùng tên ('web:1' vs 'web/1') không còn ghi đè
    attachment của nhau im lặng.
  - C19-M2 [MED]: --update/--refresh-meta đổi category/tên — user fields
    (status/submitted_flag/instance_info) phục hồi vào metadata MỚI; bản
    cũ tombstone ``superseded_by`` qua repo.update_metadata, không rm trực
    tiếp; id chỉ còn một nơi trong index.
  - C19-M3 [MED]: consent file lớn quét TRƯỚC thread pool, hỏi GỘP trên
    main thread; worker thread không bao giờ gọi input().
  - C19-M4 [MED]: skip-if-exists theo PRESENCE trừ khi force (Content-Length
    unknown/chunked không còn đè file); resume (.part) là luồng riêng.
  - C19-M5 [MED]: vòng retry có exponential backoff 0.5×2^n cap 8s + jitter;
    HEAD probe mặc định chuyển vào preflight (cache dùng lại lúc tải).
  - C19-M6 [MED]: sanitize_filename cap theo BYTE utf-8, cắt không đứt
    codepoint.
  - C19-L7 [LOW]: README/NOTE ghi atomic qua fileio helper; challenge/README
    (derived) được viết lại thay vì stale vĩnh viễn; file USER-OWNED
    (writeup README, solve.py) vẫn giữ exists-guard.
  - C19-L8 [LOW]: nhánh Mega đi qua consent/max_size gate như http.
  - C19-L9 [LOW]: phép so downloaded vs Content-Length được guard theo
    Content-Encoding (gzip/deflate/br giải nén sai ngữ nghĩa byte dây).
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import requests

from ctf_downloader.config import DownloaderConfig
from ctf_downloader.models import Challenge
from ctf_downloader.services import pull_service
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo
from ctf_downloader.generator.workspace_builder import WorkspaceBuilder
from ctf_downloader.downloaders.http_downloader import HttpDownloader
from ctf_downloader.downloaders.manager import ConsentState, DownloadManager
from ctf_downloader.downloaders.mega import MegaDownloader
from ctf_downloader.utils.sanitize import sanitize_filename


# ----------------------------------------------------------------------
# Fakes chung
# ----------------------------------------------------------------------

BINARY_HEADERS = {"Content-Type": "application/octet-stream"}


class FakeStreamResp:
    """Response giả dạng stream cho download_file / save_response_stream."""

    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = tuple(chunks)
        self.closed = False

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def iter_content(self, chunk_size=65536):
        for ch in self._chunks:
            yield ch


class FakeStdin:
    """Giả lập sys.stdin với chế độ tty / non-tty."""

    def __init__(self, isatty):
        self._is_tty = isatty

    def isatty(self):
        return self._is_tty


def make_chall(cid, name, category, points=100, **kw):
    kw.setdefault("description", f"Plain description of {name}. No links here.")
    kw.setdefault("files", [])
    return Challenge(id=cid, name=name, category=category, points=points, **kw)


class FakePlatform:
    """Platform giả cho đường run/run_update (mock detect_platform)."""

    platform_type = "generic"

    def __init__(self, challenges, title="C19CTF", url="https://inc.example.com"):
        from ctf_downloader.models import CTFInfo
        self.ctf_info = CTFInfo(title=title, url=url,
                                platform_type=self.platform_type)
        self.ctf_info.challenges = list(challenges)
        self._challenges = list(challenges)

    def authenticate(self):
        return True

    def fetch_challenges(self):
        return list(self._challenges)


class FakeDL(pull_service.DownloadManager):
    """DownloadManager giả: GIẢ LẬP tải thành công, không gọi mạng."""

    calls = []

    def download_challenge_files(self, files=None, extracted_links=None,
                                 dest_dir=None, download_third_party=True):
        type(self).calls.append((list(files or []), dest_dir))
        results = []
        for url, name in (files or []):
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, name)
            with open(path, "wb") as f:
                f.write(b"fake-download-bytes")
            results.append({"url": url, "name": name, "saved_path": path,
                            "success": True, "source": "platform_attachment"})
        del extracted_links, download_third_party
        return results


def dm_fake_patch():
    return patch.object(pull_service, "DownloadManager", FakeDL)


def detect_patch(platform):
    return patch.object(pull_service.PlatformDetector, "detect_platform",
                        return_value=platform)


# ===========================================================================
# C19-H1 — Hàm duy nhất quyết định thư mục challenge (guard owner/-id)
# ===========================================================================

class TestC19H1ResolverSingleSource(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c19h1_resolver_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_sanitized_colliding_names_get_distinct_dirs(self):
        """'web/login' vs 'web:login' sanitize trùng -> 'web_login'. Resolver
        là HÀM DUY NHẤT: chưa có chủ -> cùng base path; thư mục đã có chủ
        id khác -> bài sau tách sang hậu tố '-<id>'; cùng id -> tái sử dụng."""
        a = Challenge(id=1, name="web/login", category="Web")
        b = Challenge(id=2, name="web:login", category="Web")

        base_a = WorkspaceBuilder.resolve_challenge_dir(self.tmp, a)
        base_b = WorkspaceBuilder.resolve_challenge_dir(self.tmp, b)
        self.assertEqual(base_a, os.path.join(self.tmp, "Web", "web_login"))
        self.assertEqual(base_b, base_a,
                         "chưa ai sở hữu thì cùng ứng viên base path")

        WorkspaceBuilder.create_challenge_workspace(self.tmp, a, [], [], [])

        b2 = WorkspaceBuilder.resolve_challenge_dir(self.tmp, b)
        self.assertEqual(b2, os.path.join(self.tmp, "Web", "web_login-2"),
                         "thư mục đã thuộc về id=1 -> id=2 phải tách '-2'")

        a2 = WorkspaceBuilder.resolve_challenge_dir(self.tmp, a)
        self.assertEqual(a2, base_a, "cùng id (pull lại) tái sử dụng thư mục")

    def test_dir_without_readable_metadata_counts_as_unowned(self):
        os.makedirs(os.path.join(self.tmp, "Web", "orphan"), exist_ok=True)
        chall = Challenge(id=9, name="orphan", category="Web")
        self.assertEqual(WorkspaceBuilder.resolve_challenge_dir(self.tmp, chall),
                         os.path.join(self.tmp, "Web", "orphan"))

    def test_weird_category_falls_back_to_default_category(self):
        chall = Challenge(id=3, name="weird", category=None)
        self.assertEqual(
            WorkspaceBuilder.resolve_challenge_dir(self.tmp, chall),
            os.path.join(self.tmp, "Misc", "weird"))

    def test_builder_accepts_precomputed_challenge_dir(self):
        """Builder nhận thư mục đã tính TRƯỚC từ caller (đích tải một lần)
        thay vì tự tính lại — chống lệch giữa nơi tải và nơi dựng."""
        dest = os.path.join(self.tmp, "Crypto", "precomputed")
        chall = Challenge(id=7, name="ignored-on-precomputed", category="Crypto")
        ret = WorkspaceBuilder.create_challenge_workspace(
            self.tmp, chall, [], [], [], challenge_dir=dest)
        self.assertEqual(ret, dest)
        meta = json.load(open(os.path.join(dest, "metadata.json"),
                              encoding="utf-8"))
        self.assertEqual(meta.get("id"), 7)


class DestSpyDM(pull_service.DownloadManager):
    """Spy ghi lại dest_dir mà _full_process đưa vào cho lần tải."""

    dest_dirs = []

    def download_challenge_files(self, files=None, extracted_links=None,
                                 dest_dir=None, download_third_party=True):
        type(self).dest_dirs.append(dest_dir)
        results = []
        for url, name in (files or []):
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, name)
            with open(path, "wb") as f:
                f.write(b"NEW-BYTES-FROM-B")
            results.append({"url": url, "name": name, "saved_path": path,
                            "success": True, "source": "platform_attachment"})
        del extracted_links, download_third_party
        return results


class TestC19H1FullProcessDestination(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c19h1_fp_")
        DestSpyDM.dest_dirs = []

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_redownload_does_not_overwrite_owner_attachment(self):
        """REPRO va chạm 'web:1' vs 'web/1': challenge A (id=1, 'web/login')
        đang sở hữu Web/web_login kèm attachment. Challenge B (id=2,
        'web:login') đi qua _full_process: đích tải PHẢI được quyết bằng
        cùng hàm có guard -> rơi vào web_login-2, attachment của A NGUYÊN
        VẸN. Trước fix: _full_process tự tính sanitize() không guard ->
        tải thẳng vào Web/web_login/challenge và đè mất file của A."""
        chall_a = Challenge(id=1, name="web/login", category="Web",
                            description="alpha desc")
        WorkspaceBuilder.create_challenge_workspace(self.tmp, chall_a, [], [], [])
        owner_attach = os.path.join(self.tmp, "Web", "web_login",
                                    "challenge", "dist.zip")
        os.makedirs(os.path.dirname(owner_attach), exist_ok=True)
        with open(owner_attach, "wb") as f:
            f.write(b"A" * 64)

        chall_b = Challenge(id=2, name="web:login", category="Web",
                            description="beta desc",
                            files=[("https://cdn.example.com/dist.zip",
                                    "dist.zip")])
        cfg = DownloaderConfig(url="https://x.example.com", cookie="c=1",
                               output_dir=self.tmp)
        with patch.object(pull_service, "DownloadManager", DestSpyDM):
            PullService._full_process(cfg, object(), chall_b)

        self.assertEqual(len(DestSpyDM.dest_dirs), 1)
        dest = DestSpyDM.dest_dirs[0]
        self.assertTrue(
            dest.endswith(os.path.join("Web", "web_login-2", "challenge")),
            f"đích tải phải là thư mục đã áp guard owner/-id, thấy: {dest}")

        # Attachment của challenge A không bị đè
        with open(owner_attach, "rb") as f:
            self.assertEqual(f.read(), b"A" * 64,
                             "attachment chủ sở hữu bị thread sau ghi đè "
                             "(C19-H1)")
        # B có file của chính mình + metadata cùng thư mục
        self.assertTrue(os.path.isfile(os.path.join(dest, "dist.zip")))
        meta = json.load(open(os.path.join(self.tmp, "Web", "web_login-2",
                                           "metadata.json"),
                              encoding="utf-8"))
        self.assertEqual(meta.get("id"), 2)


# ===========================================================================
# C19-M2 — --update đổi category/tên: user fields vào metadata MỚI,
# bản cũ tombstone có kiểm
# ===========================================================================

class TestC19M2RenameUpdate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="c19m2_")
        self.out_dir = os.path.join(self._tmp, "ws")
        FakeDL.calls = []
        self.config = DownloaderConfig(
            url="https://inc.example.com", cookie="session=abc",
            output_dir=self.out_dir, threads=1)
        # Round 1 — full pull nền móng: Alpha(1) có attachment
        round1 = FakePlatform([make_chall(
            1, "Alpha", "Web", 100,
            files=[("https://inc.example.com/files/attach.zip", "attach.zip")])])
        with detect_patch(round1), dm_fake_patch():
            r = PullService.run(self.config)
        self.assertTrue(r["ok"])
        self.repo = WorkspaceRepo(self.out_dir)
        self.old_mp = self._meta_by_id("1")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _meta_by_id(self, cid, exclude=None):
        for mp in self.repo.iter_challenges():
            if exclude and os.path.abspath(mp) == os.path.abspath(exclude):
                continue
            m = self.repo.read_metadata(mp)
            if m and str(m.get("id")) == str(cid):
                return mp
        return None

    def test_rename_restores_user_fields_into_new_dir_and_tombstones_old(self):
        # User state tích luỹ trên local
        self.repo.update_status(self.old_mp, lambda st: {
            **st, "solve": "working", "notes": "my private note"})
        self.repo.update_metadata(self.old_mp, lambda m: {
            **m,
            "submitted_flag": "FLAG{user_found}",
            "instance_info": {**(m.get("instance_info") or {}),
                              "active_instance": "inst-1",
                              "is_container": True},
        })

        # Xoá attachment trên đĩa để --refresh-meta đưa Alpha vào hàng tải lại
        saved = os.path.join(os.path.dirname(self.old_mp), "challenge",
                             "attach.zip")
        self.assertTrue(os.path.isfile(saved))
        os.remove(saved)

        # Round 2 — Alpha ĐỔI category + tên trên API
        round2 = FakePlatform([make_chall(
            1, "AlphaRenamed", "Crypto", 150,
            files=[("https://inc.example.com/files/attach.zip", "attach.zip")])])
        cfg = DownloaderConfig(url="https://inc.example.com",
                               cookie="session=abc",
                               output_dir=self.out_dir, threads=1,
                               refresh_meta=True)
        with detect_patch(round2), dm_fake_patch():
            r = PullService.run_update(cfg)
        self.assertTrue(r["ok"])

        new_mp = os.path.join(self.out_dir, "Crypto", "AlphaRenamed",
                              "metadata.json")
        self.assertTrue(os.path.isfile(new_mp),
                        "builder phải dựng thư mục MỚI cho category/tên mới")

        # User fields phải sống sót ở metadata MỚI (không phải file CŨ)
        new_meta = json.load(open(new_mp, encoding="utf-8"))
        self.assertEqual(new_meta.get("submitted_flag"), "FLAG{user_found}",
                         "submitted_flag bị phục hồi nhầm vào file cũ "
                         "(C19-M2)")
        self.assertEqual((new_meta.get("instance_info") or {})
                         .get("active_instance"), "inst-1")
        st = self.repo.read_status(new_mp)
        self.assertEqual(st.get("notes"), "my private note")
        self.assertEqual(st.get("solve"), "working")

        # Bản cũ: tồn tại nhưng tombstone superseded_by -> id không còn 2 nơi
        self.assertTrue(os.path.isfile(self.old_mp),
                        "bản cũ xử lý có kiểm (tombstone), không rm trực tiếp")
        old_meta = json.load(open(self.old_mp, encoding="utf-8"))
        self.assertEqual(old_meta.get("superseded_by"), str(new_mp))

        live = self._meta_by_id("1")
        self.assertIsNotNone(live)
        self.assertEqual(os.path.abspath(live),
                         os.path.abspath(new_mp),
                         "index (bỏ tombstone) phải thấy đúng MỘT metadata "
                         "cho id=1 tại vị trí mới")

        # Attachment mới nằm ở thư mục mới
        self.assertTrue(os.path.isfile(os.path.join(
            self.out_dir, "Crypto", "AlphaRenamed", "challenge", "attach.zip")))

    def test_followup_update_sees_id_once_and_marks_nothing_missing(self):
        """Sau rename+tombstone, --update kế tiếp không coi id=1 là 'new'
        (nhận diện qua metadata mới) và không mark removed oan."""
        self.test_rename_restores_user_fields_into_new_dir_and_tombstones_old()

        round3 = FakePlatform([make_chall(
            1, "AlphaRenamed", "Crypto", 150,
            files=[("https://inc.example.com/files/attach.zip", "attach.zip")])])
        cfg = DownloaderConfig(url="https://inc.example.com",
                               cookie="session=abc",
                               output_dir=self.out_dir, threads=1)
        with detect_patch(round3), dm_fake_patch():
            r = PullService.run_update(cfg)
        self.assertEqual(r["new"], 0,
                         "id=1 đã có local (bản mới) — không được tính new")
        self.assertEqual(r["missing"], 0)


# ===========================================================================
# C19-M3 — Consent file lớn: preflight main thread, worker không input()
# ===========================================================================

SIZES = {"https://x/big1.bin": 3000, "https://x/big2.bin": 5000,
         "https://x/small.bin": 10}


def fake_probe(url, session=None, timeout=30):
    return SIZES.get(url)


class TestC19M3ConsentPreflight(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c19m3_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plan_consents_asks_once_for_whole_batch(self):
        state = ConsentState()
        mgr = DownloadManager(session=MagicMock(), size_limit_bytes=1000,
                              consent_state=state)
        with patch.object(HttpDownloader, "probe_content_length",
                          staticmethod(fake_probe)), \
             patch("sys.stdin", FakeStdin(True)), \
             patch("builtins.input", return_value="y") as mock_input:
            mgr.plan_consents(["https://x/big1.bin", "https://x/small.bin",
                               "https://x/big2.bin", "https://x/big1.bin"])
        self.assertEqual(mock_input.call_count, 1,
                         "phải hỏi GỘP một lần thay vì từng file")
        self.assertIn("[y/N]", mock_input.call_args[0][0])
        self.assertEqual(state.decisions,
                         {"https://x/big1.bin": True,
                          "https://x/big2.bin": True})
        # Dung lượng probe đúng một lần, cache lại cho lúc tải
        self.assertEqual(state.sizes.get("https://x/small.bin"), 10)

    def test_denied_preflight_decision_skips_download_without_prompt(self):
        state = ConsentState()
        mgr = DownloadManager(session=MagicMock(), size_limit_bytes=1000,
                              consent_state=state)
        with patch.object(HttpDownloader, "probe_content_length",
                          staticmethod(fake_probe)), \
             patch("sys.stdin", FakeStdin(True)), \
             patch("builtins.input", return_value="n"):
            mgr.plan_consents(["https://x/big1.bin"])

        worker = DownloadManager(session=MagicMock(), size_limit_bytes=1000,
                                 consent_state=state)
        with patch("builtins.input", side_effect=AssertionError(
                "worker phải tôn trọng quyết định preflight, không input()")), \
             patch("sys.stdin", FakeStdin(False)):
            ok, path, msg = worker.download_url("https://x/big1.bin", self.tmp,
                                                link_type="direct_file")
        self.assertFalse(ok)
        self.assertIn("skipped_large_file", msg)
        self.assertIsNone(path)

    def test_worker_thread_without_preflight_never_calls_input(self):
        """input() từ WORKER thread chồng prompt lên nhau (C19-M3): thread
        thường không có quyết định preflight phải tự skip, không hỏi."""
        mgr = DownloadManager(session=MagicMock(), size_limit_bytes=1000)
        result = {}

        def run():
            result["allowed"] = mgr._confirm_large_download(
                "https://x/big.bin", 5000)

        with patch("sys.stdin", FakeStdin(True)), \
             patch("builtins.input", side_effect=AssertionError(
                 "không được input() ngoài main thread")):
            t = threading.Thread(target=run)
            t.start()
            t.join(5)
        self.assertIs(result.get("allowed"), False,
                      "worker thread phải tự skip thay vì mở prompt")

    def test_main_thread_direct_call_stays_interactive(self):
        """Tương thích: gọi trực tiếp trên main thread (ngoài pipeline pull)
        vẫn qua interactive consent như cũ."""
        mgr = DownloadManager(session=MagicMock(), size_limit_bytes=1000)
        with patch("sys.stdin", FakeStdin(True)), \
             patch("builtins.input", return_value="y") as mock_input:
            self.assertTrue(mgr._confirm_large_download(
                "https://x/big.bin", 5000))
        mock_input.assert_called_once()


# ===========================================================================
# C19-M4 — Skip-if-exists theo presence trừ khi force
# ===========================================================================

class TestC19M4PresenceSkip(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c19m4_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unknown_length_response_does_not_overwrite_existing(self):
        """Response không khai báo Content-Length (chunked): điều kiện skip
        cũ đòi total_size > 0 nên vẫn TẢI LẠI và ĐÈ file hoàn chỉnh đã có."""
        target = os.path.join(self.tmp, "f.bin")
        with open(target, "wb") as f:
            f.write(b"OLD-COMPLETE")

        resp = FakeStreamResp(200, dict(BINARY_HEADERS), chunks=(b"NEW",))
        session = MagicMock()
        session.get.return_value = resp
        saved = HttpDownloader.download_file(
            "https://x/f.bin", self.tmp, session, preferred_filename="f.bin",
            force=False)

        self.assertEqual(saved, target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"OLD-COMPLETE",
                             "force=False không được đè file đã có dù "
                             "Content-Length unknown (C19-M4)")
        # Skip phải xảy ra TRƯỚC khi phát bất kỳ GET nào
        session.get.assert_not_called()
        self.assertNotIn("f.bin.part", os.listdir(self.tmp))

    def test_force_true_overwrites_existing_file(self):
        target = os.path.join(self.tmp, "f.bin")
        with open(target, "wb") as f:
            f.write(b"OLD")
        resp = FakeStreamResp(200, {**BINARY_HEADERS, "Content-Length": "3"},
                              chunks=(b"NEW",))
        session = MagicMock()
        session.get.return_value = resp
        saved = HttpDownloader.download_file(
            "https://x/f.bin", self.tmp, session, preferred_filename="f.bin",
            force=True)
        self.assertEqual(saved, target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"NEW")

    def test_save_stream_presence_skip_without_content_length(self):
        target = os.path.join(self.tmp, "s.bin")
        with open(target, "wb") as f:
            f.write(b"OLD")
        resp = FakeStreamResp(200, dict(BINARY_HEADERS), chunks=(b"NEW",))
        saved = HttpDownloader.save_response_stream(resp, self.tmp, "s.bin",
                                                    force=False)
        self.assertEqual(saved, target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"OLD",
                             "save_response_stream unknown-length không được "
                             "ghi đè (C19-M4)")

    def test_save_stream_force_true_overwrites(self):
        target = os.path.join(self.tmp, "s.bin")
        with open(target, "wb") as f:
            f.write(b"OLD")
        resp = FakeStreamResp(200, {**BINARY_HEADERS, "Content-Length": "3"},
                              chunks=(b"NEW",))
        saved = HttpDownloader.save_response_stream(resp, self.tmp, "s.bin",
                                                    force=True)
        self.assertEqual(saved, target)
        with open(target, "rb") as f:
            self.assertEqual(f.read(), b"NEW")

    def test_resume_flow_kept_intact_when_part_exists(self):
        """.part tồn tại = lần tải trước CHƯA xong -> resume riêng vẫn chạy
        (presence-skip không được ăn theo làm mất resume)."""
        part = os.path.join(self.tmp, "r.bin.part")
        with open(part, "wb") as f:
            f.write(b"P" * 200)
        resp = FakeStreamResp(206, {**BINARY_HEADERS, "Content-Length": "100"},
                              chunks=(b"R" * 100,))
        session = MagicMock()
        session.get.return_value = resp
        saved = HttpDownloader.download_file(
            "https://x/r.bin", self.tmp, session, preferred_filename="r.bin",
            force=False)
        self.assertIsNotNone(saved)
        with open(saved, "rb") as f:
            self.assertEqual(f.read(), b"P" * 200 + b"R" * 100)
        self.assertFalse(os.path.exists(part))


# ===========================================================================
# C19-M5 — Retry backoff exponential (0.5s×2^n cap 8s + jitter nhẹ)
# ===========================================================================

class TestC19M5RetryBackoff(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c19m5_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_connection_error_retries_sleep_exponential_backoff(self):
        """Trước fix: vòng retry `continue` NGAY sau khi tăng attempt —
        bắn liên hoàn vào server đang quá tải. Sau fix: mỗi lần thử lại
        ngủ 0.5×2^(n-1)s (+ jitter nhẹ)."""
        ok = FakeStreamResp(200, {**BINARY_HEADERS, "Content-Length": "4"},
                            chunks=(b"DATA",))
        session = MagicMock()
        session.get.side_effect = [
            requests.ConnectionError("boom1"),
            requests.ConnectionError("boom2"),
            ok,
        ]
        sleeps = []
        with patch("ctf_downloader.downloaders.http_downloader.time.sleep",
                   side_effect=sleeps.append):
            saved = HttpDownloader.download_file(
                "https://x/d.bin", self.tmp, session,
                preferred_filename="d.bin")
        self.assertIsNotNone(saved)
        self.assertEqual(len(sleeps), 2,
                         f"hai lần lỗi phải có đúng 2 lần backoff, thấy "
                         f"{sleeps}")
        self.assertGreaterEqual(sleeps[0], 0.5)
        self.assertGreater(sleeps[1], sleeps[0],
                           "backoff phải tăng theo attempt (exponential)")
        self.assertTrue(all(s <= 8.25 for s in sleeps),
                        f"cap 8s + jitter ≤0.25s, thấy {sleeps}")

    def test_backoff_helper_caps_at_8_seconds(self):
        with patch("ctf_downloader.downloaders.http_downloader.time.sleep",
                   return_value=None) as mock_sleep:
            delay = HttpDownloader._retry_backoff(50)
        mock_sleep.assert_called_once()
        self.assertLessEqual(delay, 8.25)
        self.assertGreaterEqual(delay, 8.0)


# ===========================================================================
# C19-M6 — sanitize_filename cap theo BYTE utf-8
# ===========================================================================

class TestC19M6FilenameByteCap(unittest.TestCase):
    def test_multibyte_name_capped_within_byte_budget(self):
        name = "🔥" * 100   # 100 ký tự = 400 byte > NAME_MAX 255
        out = sanitize_filename(name)
        raw = out.encode("utf-8")
        self.assertLessEqual(len(raw), 120,
                             "max_length phải tính theo BYTE utf-8 (C19-M6)")
        # Cắt không đứt codepoint: chuỗi trả về luôn encode/decode sạch
        self.assertEqual(raw.decode("utf-8"), out)

    def test_ascii_truncation_unchanged(self):
        self.assertEqual(sanitize_filename("a" * 300), "a" * 120)

    def test_custom_budget_cuts_on_codepoint_boundary(self):
        out = sanitize_filename("é" * 80, max_length=81)   # é = 2 byte
        self.assertLessEqual(len(out.encode("utf-8")), 81)
        self.assertEqual(out, "é" * 40,
                         "81 byte lẻ 1 -> rơi 1 byte cuối, bỏ nguyên cặp "
                         "'é' thứ 41 thay vì sinh byte lỗi")

    def test_cut_to_empty_falls_back_to_default(self):
        self.assertEqual(sanitize_filename("🔥abc", max_length=1),
                         "attachment.bin")


# ===========================================================================
# C19-L7 — Section files ghi atomic; challenge/README (derived) refresh
# ===========================================================================

class TestC19L7SectionFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c19l7_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _build(self, description):
        chall = Challenge(id=11, name="sec", category="Misc",
                          description=description)
        return chall, WorkspaceBuilder.create_challenge_workspace(
            self.tmp, chall, [], [], [])

    def _challenge_readme(self, chall_dir):
        return os.path.join(chall_dir, "challenge", "README.md")

    def test_derived_challenge_readme_refreshes_on_rebuild(self):
        """README trong challenge/ là dữ liệu DERIVED — exists-guard cũ khiến
        nó KHÔNG BAO GIỜ được viết lại: sau --update metadata mới (points/
        description đổi) nhưng README vẫn stale vĩnh viễn."""
        _, chall_dir = self._build("marker V1-DESCRIPTION")
        readme = self._challenge_readme(chall_dir)
        self.assertIn("V1-DESCRIPTION", open(readme, encoding="utf-8").read())

        self._build("marker V2-DESCRIPTION")
        content = open(readme, encoding="utf-8").read()
        self.assertIn("V2-DESCRIPTION", content,
                      "README derived phải refresh khi dựng lại (C19-L7)")
        self.assertNotIn("V1-DESCRIPTION", content)

    def test_user_owned_writeup_and_solve_survive_rebuild(self):
        chall, chall_dir = self._build("desc")
        writeup = os.path.join(chall_dir, "writeup", "README.md")
        solve = os.path.join(chall_dir, "solver", "solve.py")
        with open(writeup, "w", encoding="utf-8") as f:
            f.write("# User writeup\nFLAG{user_wrote_this}\n")
        with open(solve, "w", encoding="utf-8") as f:
            f.write("# user exploit\nprint('do not clobber')\n")

        self._build("desc rebuilt")
        self.assertIn("FLAG{user_wrote_this}",
                      open(writeup, encoding="utf-8").read(),
                      "file USER-OWNED phải giữ exists-guard")
        self.assertIn("do not clobber",
                      open(solve, encoding="utf-8").read())

    def test_producer_failure_keeps_existing_readme(self):
        _, chall_dir = self._build("good V1 content")
        readme = self._challenge_readme(chall_dir)
        before = open(readme, encoding="utf-8").read()

        with patch.object(WorkspaceBuilder, "_generate_readme",
                          side_effect=RuntimeError("boom")):
            self._build("unreachable description")
        after = open(readme, encoding="utf-8").read()
        self.assertEqual(after, before,
                         "producer raise trên file ĐÃ CÓ nội dung tốt thì "
                         "giữ nguyên bản cũ, không đè bằng trang lỗi")

    def test_no_tmp_or_lock_residue_after_build(self):
        _, chall_dir = self._build("clean build")
        residue = []
        for root, _dirs, files in os.walk(chall_dir):
            for fn in files:
                if fn.endswith(".tmp") or fn.endswith(".lock"):
                    residue.append(os.path.join(root, fn))
        self.assertEqual(residue, [],
                         "ghi atomic không được để lại .tmp/.lock (C19-L7)")


# ===========================================================================
# C19-L8 — Nhánh Mega đi qua consent/max_size gate như http
# ===========================================================================

class TestC19L8MegaGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c19l8_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_mega_denied_by_preflight_skips_without_download(self):
        state = ConsentState()
        url = "https://mega.nz/file/xyz"
        state.sizes[url] = 999999
        state.decisions[url] = False
        mgr = DownloadManager(session=MagicMock(), size_limit_bytes=1000,
                              consent_state=state)
        with patch.object(MegaDownloader, "available_tool",
                          MagicMock(return_value="/usr/bin/megadl")) as tool, \
             patch.object(MegaDownloader, "download",
                          MagicMock(side_effect=AssertionError(
                              "bị từ chối consent rồi, không được tải"))):
            ok, path, msg = mgr.download_url(url, self.tmp, link_type="mega")
        self.assertFalse(ok)
        self.assertIn("skipped_large_file", msg,
                      "Mega phải đi qua consent gate như http (C19-L8)")
        tool.assert_called_once()   # tool vẫn được kiểm tra trước

    def test_mega_unknown_size_passes_gate_and_downloads(self):
        state = ConsentState()
        url = "https://mega.nz/file/xyz"
        state.sizes[url] = None
        mgr = DownloadManager(session=MagicMock(), size_limit_bytes=1000,
                              consent_state=state)
        saved = os.path.join(self.tmp, "out.bin")
        with patch.object(MegaDownloader, "available_tool",
                          MagicMock(return_value="/usr/bin/megadl")), \
             patch.object(MegaDownloader, "download",
                          MagicMock(return_value=(saved, "ok"))) as dl:
            ok, path, msg = mgr.download_url(url, self.tmp, link_type="mega")
        self.assertTrue(ok)
        self.assertEqual(path, saved)
        dl.assert_called_once()


# ===========================================================================
# C19-L9 — Guard phép so downloaded vs Content-Length theo Content-Encoding
# ===========================================================================

class TestC19L9EncodingGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="c19l9_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_gzip_body_shorter_than_wire_cl_succeeds(self):
        """Content-Length là cỡ NÉN trên dây; iter_content trả bytes ĐÃ GIẢI
        NÉN. Dữ liệu khó-nén giải ra NHỎ HƠN CL -> phép so cũ báo 'thiếu dữ
        liệu' ảo rồi retry đến chết dù file đủ (C19-L9)."""
        resp = FakeStreamResp(
            200,
            {**BINARY_HEADERS, "Content-Length": "100",
             "Content-Encoding": "gzip"},
            chunks=(b"D" * 90,))
        session = MagicMock()
        session.get.return_value = resp
        saved = HttpDownloader.download_file(
            "https://x/g.bin", self.tmp, session, preferred_filename="g.bin")
        self.assertIsNotNone(saved,
                             "response gzip đủ dữ liệu không được báo thất bại")
        self.assertEqual(os.path.getsize(saved), 90)


if __name__ == "__main__":
    unittest.main()
