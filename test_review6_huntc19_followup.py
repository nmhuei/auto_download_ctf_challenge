"""Review-6 follow-up cho commit hunt-c19 — 6 finding còn lại:

1. [HIGH] Tombstone ``superseded_by`` chỉ được lọc trong pull_service; mọi
   reader khác vẫn khớp thư mục chết theo id. Fix hướng tập trung: helper
   ``is_superseded`` + lọc ở TẦNG REPO (challenge_index/find_challenge),
   các đường tự đi os.walk gọi cùng helper.
2. [MED] summary_generator tự tính cat/name không qua guard C9-01 ->
   phải đi qua ``WorkspaceBuilder.resolve_challenge_dir``.
3. [MED] http_downloader nuốt file 0-byte khi body rỗng + không
   Content-Length (move .part rỗng lên đích rồi presence-skip vĩnh viễn).
4. [MED] sanitize.py nổ UnicodeEncodeError với lone surrogate (U+D800).
5. [LOW] plan_consents HEAD-probe tuần tự không throttle.
6. [LOW] cache SubmitService lưu key id và key tên cùng chuỗi — name-key
   phải thắng id-key khi trùng (khớp quy ước batch-3).

TDD: các test tái hiện bug TRƯỚC khi sửa (RED). Mọi network bị mock.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import unittest.mock
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ctf_downloader.storage.workspace_repo import WorkspaceRepo


# ----------------------------------------------------------------------
# Helpers chung
# ----------------------------------------------------------------------

def make_tombstone_ws(prefix="rev6_tomb_"):
    """Workspace mô phỏng rename A→B rồi tombstone: HAI thư mục cùng id=1;
    thư mục cũ ('old_name') mang superseded_by trỏ tới metadata mới."""
    ws = tempfile.mkdtemp(prefix=prefix)
    with open(os.path.join(ws, "challenges.json"), "w", encoding="utf-8") as f:
        json.dump({
            "platform_url": "http://ctf.test",
            "ctf_info": {"url": "http://ctf.test",
                         "flag_format": "^FLAG\\{.+\\}$",
                         "flag_format_source": "cache"},
            "challenges": [{"id": 1, "name": "new_name"}],
        }, f)
    old_dir = os.path.join(ws, "Web", "old_name")
    new_dir = os.path.join(ws, "Web", "new_name")
    os.makedirs(old_dir)
    os.makedirs(new_dir)
    with open(os.path.join(old_dir, "metadata.json"), "w",
              encoding="utf-8") as f:
        json.dump({"id": 1, "name": "old_name",
                   "superseded_by": os.path.join(new_dir, "metadata.json")},
                  f)
    with open(os.path.join(new_dir, "metadata.json"), "w",
              encoding="utf-8") as f:
        json.dump({"id": 1, "name": "new_name"}, f)
    return ws, old_dir, new_dir


def make_submit_svc(ws):
    """SubmitService với platform/detector bị mock (không network)."""
    from ctf_downloader.services.submit_service import SubmitService

    platform = MagicMock()
    platform.ctf_info.platform_type = "ctfd"
    platform.authenticate.return_value = True
    with unittest.mock.patch(
            "ctf_downloader.services.submit_service.create_session",
            return_value=MagicMock()), \
         unittest.mock.patch(
            "ctf_downloader.services.submit_service.PlatformDetector"
            ".detect_platform", return_value=platform):
        svc = SubmitService(url="http://ctf.test", workspace_dir=ws)
    return svc, platform


def write_status_flag(meta_path, value="FLAG{x}", state="hoarded"):
    """Ghi block status.flag vào metadata.json (giả lập flag đã hoard)."""
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    meta["status"] = {"flag": {"value": value, "state": state}}
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f)


# ======================================================================
# FINDING 1 [HIGH] — tombstone lọc tập trung ở tầng repo + mọi reader
# ======================================================================

class TestRepoTombstoneFiltering(unittest.TestCase):
    def setUp(self):
        self.ws, self.old_dir, self.new_dir = make_tombstone_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        # Làm rỗng mảng challenges trong challenges.json để find_challenge
        # phải resolve từ metadata.json TRÊN ĐỘI (entry JSON luôn thắng tier
        # exact-id và không mang _local_path — behavior nguyên vẹn ngoài phạm
        # vi finding này).
        with open(os.path.join(self.ws, "challenges.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"ctf_info": {"url": "http://ctf.test"},
                       "challenges": []}, f)
        self.repo = WorkspaceRepo(self.ws)

    def test_helper_is_superseded(self):
        # Helper dùng chung nhận dict/None/rác an toàn.
        from ctf_downloader.storage.workspace_repo import is_superseded
        self.assertTrue(is_superseded({"superseded_by": "/x/metadata.json"}))
        self.assertFalse(is_superseded({"id": 1}))
        self.assertFalse(is_superseded(None))
        self.assertFalse(is_superseded("junk"))

    def test_challenge_index_skips_tombstone(self):
        entries = self.repo.challenge_index()
        local = [e["_local_path"] for e in entries if "_local_path" in e]
        self.assertEqual([self.new_dir], local,
                         "index phải bỏ bản tombstone (thư mục chết)")

    def test_find_challenge_returns_live_dir_only(self):
        hit = self.repo.find_challenge(1)
        self.assertIsNotNone(hit)
        self.assertEqual(self.new_dir, hit.get("_local_path"))
        # Tên cũ thuộc bản chết -> không khớp nữa
        self.assertIsNone(self.repo.find_challenge("old_name"))
        # Tên mới vẫn tra được
        self.assertEqual(self.new_dir,
                         self.repo.find_challenge("new_name")["_local_path"])


class TestSubmitServiceTombstone(unittest.TestCase):
    def setUp(self):
        self.ws, self.old_dir, self.new_dir = make_tombstone_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)

    def test_find_meta_path_resolves_live_dir(self):
        svc, _platform = make_submit_svc(self.ws)
        mp = svc._find_meta_path(1)
        self.assertEqual(os.path.join(self.new_dir, "metadata.json"),
                         str(mp),
                         "submit phải ghi phán quyết vào dir sống, không "
                         "phải dir chết theo id")

    def test_hoard_flag_writes_into_live_dir_only(self):
        svc, _platform = make_submit_svc(self.ws)
        ok, msg = svc.hoard_flag(1, "FLAG{hoard}")
        self.assertTrue(ok, msg)
        new_meta = json.load(open(os.path.join(self.new_dir,
                                               "metadata.json"),
                                  encoding="utf-8"))
        self.assertEqual("hoarded", new_meta["status"]["flag"]["state"])
        old_meta = json.load(open(os.path.join(self.old_dir,
                                               "metadata.json"),
                                  encoding="utf-8"))
        self.assertNotIn("status", old_meta,
                         "dir chết KHÔNG được bị ghi")


class TestStatusServiceTombstone(unittest.TestCase):
    def setUp(self):
        self.ws, self.old_dir, self.new_dir = make_tombstone_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.repo = WorkspaceRepo(self.ws)

    def test_scan_local_challenges_excludes_tombstone(self):
        from ctf_downloader.services.status_service import StatusService
        rows = StatusService.scan_local_challenges(self.repo)
        names = [r.get("name") for r in rows]
        self.assertNotIn("old_name", names)
        self.assertIn("new_name", names)

    def test_resolve_challenge_single_hit_is_live_dir(self):
        from ctf_downloader.services.status_service import StatusService
        meta_path, meta = StatusService.resolve_challenge(self.repo, "1")
        self.assertEqual(self.new_dir, str(meta_path.parent))
        self.assertEqual("new_name", meta.get("name"))


class TestHoardListNoDuplication(unittest.TestCase):
    def setUp(self):
        self.ws, self.old_dir, self.new_dir = make_tombstone_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        # Flag hoarded xuất hiện ở CẢ hai bản (kịch bản thật: user hoard
        # trước khi pull --update rename + restore user-fields).
        write_status_flag(os.path.join(self.old_dir, "metadata.json"))
        write_status_flag(os.path.join(self.new_dir, "metadata.json"))

    def test_collect_hoarded_single_row_per_challenge(self):
        from ctf_downloader.cli_commands import _collect_hoarded
        rows = _collect_hoarded(WorkspaceRepo(self.ws))
        self.assertEqual(1, len(rows),
                         f"hoard --list nhân đôi hàng tombstone: {rows}")
        self.assertEqual("new_name", rows[0]["name"])


class TestInstanceServiceTombstone(unittest.TestCase):
    def setUp(self):
        self.ws, self.old_dir, self.new_dir = make_tombstone_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        from ctf_downloader.services.instance_service import InstanceService
        svc = InstanceService.__new__(InstanceService)
        svc.workspace_path = self.ws
        svc.cookie = svc.token = None
        svc.repo = WorkspaceRepo(self.ws)
        svc.challenges_data = {}
        svc.platform = MagicMock()
        self.svc = svc

    def test_find_challenge_fallback_resolves_live_dir(self):
        chall = self.svc.find_challenge(challenge_id=1)
        self.assertIsNotNone(chall)
        self.assertEqual(self.new_dir, chall.get("_local_path"))

    def test_list_containers_skips_tombstone(self):
        # Biến cả hai thành container để thấy list cũng bị nhân đôi.
        for d in (self.old_dir, self.new_dir):
            p = os.path.join(d, "metadata.json")
            meta = json.load(open(p, encoding="utf-8"))
            meta["instance_info"] = {"is_container": True}
            json.dump(meta, open(p, "w", encoding="utf-8"))
        rows = self.svc.list_containers()
        self.assertEqual([self.new_dir],
                         [r.get("_local_path") for r in rows])

    def test_update_local_instance_info_skips_tombstone(self):
        self.svc._update_local_instance_info(1, entry="h:p", time_left=10,
                                             status="running")
        old_meta = json.load(open(os.path.join(self.old_dir, "metadata.json"),
                                  encoding="utf-8"))
        self.assertNotIn("instance_info", old_meta,
                         "dir chết KHÔNG được ghi instance_info")
        new_meta = json.load(open(os.path.join(self.new_dir, "metadata.json"),
                                  encoding="utf-8"))
        self.assertEqual("running", new_meta["instance_info"]["status"])


class TestPullSupersededSingleSource(unittest.TestCase):
    def test_pull_service_delegates_to_repo_helper(self):
        # Drift guard: một nguồn truth duy nhất cho predicate tombstone.
        from ctf_downloader.services.pull_service import PullService
        self.assertTrue(PullService._is_superseded({"superseded_by": "x"}))
        self.assertFalse(PullService._is_superseded({"id": 1}))


# ======================================================================
# FINDING 2 [MED] — SUMMARY đi qua resolver chung resolve_challenge_dir
# ======================================================================

class TestSummaryUsesResolver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rev6_summary_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # Thư mục 'Misc/web' ĐÃ có chủ khác (id 999) -> resolver phải
        # redirect challenge id 42 sang 'Misc/web-42'.
        occupied = os.path.join(self.tmp, "Misc", "web")
        os.makedirs(occupied)
        with open(os.path.join(occupied, "metadata.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"id": 999, "name": "web"}, f)

    def test_summary_links_follow_resolved_dir(self):
        from ctf_downloader.generator.summary_generator import SummaryGenerator
        from ctf_downloader.models import CTFInfo, Challenge

        info = CTFInfo(title="Rev6CTF", url="http://ctf.test",
                       platform_type="ctfd")
        chall = Challenge(id=42, name="web", category="Misc", points=100,
                          description="plain text without links")
        info.challenges = [chall]
        SummaryGenerator.generate_summary(self.tmp, info, {})
        summary = open(os.path.join(self.tmp, "SUMMARY.md"),
                       encoding="utf-8").read()
        self.assertIn("Misc/web-42/writeup/README.md", summary,
                      "SUMMARY phải trỏ vào dir SAU guard -<id>, không phải "
                      "dir của chủ sở hữu khác")
        self.assertIn("[`Misc/web-42`](Misc/web-42)", summary)


# ======================================================================
# FINDING 3 [MED] — body rỗng + không Content-Length là fail-retryable
# ======================================================================

class _FakeStreamResp:
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


class TestEmptyBodyUnknownLength(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="rev6_empty_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_unknown_length_empty_body_retries_then_fails(self):
        from ctf_downloader.downloaders.http_downloader import HttpDownloader

        session = MagicMock()
        session.get.side_effect = lambda *a, **k: _FakeStreamResp(
            200, {}, [])   # body rỗng hoàn toàn, không Content-Length
        with unittest.mock.patch.object(HttpDownloader, "_retry_backoff",
                                        staticmethod(lambda attempt: 0.0)):
            saved = HttpDownloader.download_file(
                "https://x/data.bin", self.tmp, session)
        self.assertIsNone(saved,
                          "0-byte không rõ độ dài phải coi là chưa hoàn thành")
        self.assertEqual([], os.listdir(self.tmp),
                         ".part rỗng không được move lên đích hay để sót")

    def test_explicit_zero_content_length_accepted(self):
        # Server khai báo RÕ Content-Length: 0 -> file 0-byte hợp lệ.
        from ctf_downloader.downloaders.http_downloader import HttpDownloader

        session = MagicMock()
        session.get.side_effect = lambda *a, **k: _FakeStreamResp(
            200, {"Content-Length": "0",
                  "Content-Type": "application/octet-stream"}, [])
        saved = HttpDownloader.download_file(
            "https://x/empty.bin", self.tmp, session)
        self.assertIsNotNone(saved)
        self.assertTrue(os.path.isfile(saved))
        self.assertEqual(0, os.path.getsize(saved))

    def test_normal_download_unaffected(self):
        from ctf_downloader.downloaders.http_downloader import HttpDownloader

        session = MagicMock()
        session.get.side_effect = lambda *a, **k: _FakeStreamResp(
            200, {"Content-Length": "5",
                  "Content-Type": "application/octet-stream"},
            [b"hello"])
        saved = HttpDownloader.download_file(
            "https://x/ok.bin", self.tmp, session)
        self.assertIsNotNone(saved)
        with open(saved, "rb") as f:
            self.assertEqual(b"hello", f.read())


# ======================================================================
# FINDING 4 [MED] — lone surrogate không được làm crash sanitize
# ======================================================================

SURROGATE = chr(0xD800)   # lone surrogate từ JSON platform hỏng


class TestSanitizeLoneSurrogate(unittest.TestCase):
    def test_folder_name_lone_surrogate_no_crash(self):
        from ctf_downloader.utils.sanitize import sanitize_folder_name
        out = sanitize_folder_name(f"web{SURROGATE}pwn")
        self.assertIsInstance(out, str)
        out.encode("utf-8")   # không được nổ UnicodeEncodeError sau này
        self.assertNotIn(SURROGATE, out)

    def test_filename_lone_surrogate_no_crash(self):
        from ctf_downloader.utils.sanitize import sanitize_filename
        out = sanitize_filename(f"report{SURROGATE}.txt")
        self.assertIsInstance(out, str)
        out.encode("utf-8")

    def test_json_platform_payload_with_surrogate(self):
        # Kênh vào thật: platform trả JSON chứa lone surrogate escaped
        # (\ud800 trong payload — parse bằng json.loads như pipeline thật).
        from ctf_downloader.utils.sanitize import (
            sanitize_folder_name,
            sanitize_filename,
        )
        meta = json.loads(
            '{"name": "bad\\u%sname", "files": ["a\\u%s.zip"]}'
            % ("D800", "D800"))
        folder = sanitize_folder_name(meta["name"], default="chall_1")
        folder.encode("utf-8")
        fname = sanitize_filename(meta["files"][0])
        fname.encode("utf-8")

    def test_normal_names_still_intact(self):
        from ctf_downloader.utils.sanitize import sanitize_folder_name
        self.assertEqual("café_grind", sanitize_folder_name("café grind"))
        self.assertEqual("web_login", sanitize_folder_name("web/login"))


# ======================================================================
# FINDING 5 [LOW] — HEAD probe tuần tự trong preflight cần khoảng nghỉ
# ======================================================================

class TestPlanConsentsPacing(unittest.TestCase):
    def _manager(self, sizes=None):
        from ctf_downloader.downloaders.manager import (ConsentState,
                                                        DownloadManager)
        state = ConsentState()
        state.sizes.update(sizes or {})
        return DownloadManager(session=MagicMock(), size_limit_bytes=0,
                               consent_state=state), state

    def test_sleep_between_probes(self):
        from ctf_downloader.downloaders import manager as mgr_mod
        from ctf_downloader.downloaders.manager import DownloadManager

        mgr = DownloadManager(session=MagicMock(), size_limit_bytes=0)
        sleeps = []
        with unittest.mock.patch.object(
                mgr_mod.HttpDownloader, "probe_content_length",
                staticmethod(lambda url, session=None, timeout=30: None)), \
             unittest.mock.patch.object(mgr_mod.time, "sleep",
                                        side_effect=sleeps.append):
            mgr.plan_consents(["https://x/1.bin", "https://x/2.bin",
                               "https://x/3.bin"])
        self.assertEqual(2, len(sleeps),
                         "phải nghỉ giữa các probe tuần tự (N-1 lần cho N "
                         f"URL probe mới); got {sleeps}")

    def test_cached_sizes_skip_pacing(self):
        from ctf_downloader.downloaders import manager as mgr_mod
        mgr, _state = self._manager(sizes={
            "https://x/1.bin": None, "https://x/2.bin": None})
        sleeps = []
        with unittest.mock.patch.object(mgr_mod.time, "sleep",
                                        side_effect=sleeps.append):
            mgr.plan_consents(["https://x/1.bin", "https://x/2.bin"])
        self.assertEqual([], sleeps,
                         "size đã cache -> không probe -> không cần nghỉ")


# ======================================================================
# FINDING 6 [LOW] — cache SubmitService: name-key thắng id-key trùng chuỗi
# ======================================================================

def make_numeric_collision_ws(prefix="rev6_numname_"):
    """{id:1337, name:'Baby Web'} + {id:42, name:'1337'} — key '1337' vừa
    là id-key của bài đầu vừa là name-key của bài sau."""
    ws = tempfile.mkdtemp(prefix=prefix)
    with open(os.path.join(ws, "challenges.json"), "w", encoding="utf-8") as f:
        json.dump({
            "platform_url": "http://ctf.test",
            "ctf_info": {"url": "http://ctf.test"},
            "challenges": [{"id": 1337, "name": "Baby Web"},
                           {"id": 42, "name": "1337"}],
        }, f)
    return ws


class TestCacheNumericNameCollision(unittest.TestCase):
    def setUp(self):
        self.ws = make_numeric_collision_ws()
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        self.svc, _platform = make_submit_svc(self.ws)

    def test_name_key_wins_over_id_key(self):
        entry = self.svc.challenges_cache.get("1337")
        self.assertIsNotNone(entry)
        self.assertEqual(42, entry.get("id"),
                         "name-key '1337' (của id 42) phải thắng id-key "
                         "cùng chuỗi bất kể thứ tự chèn")

    def test_id_route_not_contaminated_by_name_owner(self):
        # ``ctf submit <digits>`` đi qua CLI đã phân loại là ID tường minh:
        # id-route phải giữ nguyên id đích và KHÔNG rò tên/id của challenge
        # đang chiếm key theo tên vào kết quả.
        cid, name = self.svc.resolve_challenge_id("1337")
        self.assertEqual(1337, cid)
        self.assertEqual("Challenge_1337", name,
                         "entry dưới key '1337' thuộc challenge khác "
                         "(name-key thắng chỗ) — không được dùng làm tên")

    def test_name_route_still_resolves_by_name(self):
        # Cache name-key thắng chỗ -> tra theo tên "1337" (qua helper hoard
        # của CLI, đọc cache[name]) ra đúng chủ sở hữu id 42.
        self.assertEqual(42,
                         self.svc.challenges_cache["1337"].get("id"))
        # Tên thường không trùng id nào vẫn resolve như cũ
        cid2, _ = self.svc.resolve_challenge_id("baby web")
        self.assertEqual(1337, cid2)

    def test_hoard_identifier_helper_sees_name_owner(self):
        from ctf_downloader.cli_commands import _hoard_identifier
        self.assertEqual(42, _hoard_identifier(self.svc, None, "1337"))

    def test_non_colliding_cache_unchanged(self):
        ws = tempfile.mkdtemp(prefix="rev6_plain_")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        with open(os.path.join(ws, "challenges.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"ctf_info": {"url": "http://ctf.test"},
                       "challenges": [{"id": 7, "name": "Solo"}]}, f)
        svc, _platform = make_submit_svc(ws)
        self.assertEqual((7, "Solo"), svc.resolve_challenge_id("7"))
        self.assertEqual((7, "Solo"), svc.resolve_challenge_id("Solo"))


if __name__ == "__main__":
    unittest.main()
