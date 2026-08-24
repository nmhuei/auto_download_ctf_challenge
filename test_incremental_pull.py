"""
Incremental pull (--update / --refresh-meta) — unit/integration tests.

Chạy: python3 -m pytest test_incremental_pull.py -q
Toàn bộ HTTP được mock — KHÔNG gọi mạng thật (challenge không có attachment,
DownloadManager không thực hiện request nào).

Phủ 4 case acceptance criteria:
  - new      : challenge mới trên API → full pipeline (tải + dựng workspace)
  - updated  : challenge đã có → KHÔNG tải lại attachment, chỉ cập nhật
               metadata động (points/solves/connection/solved raise-only)
  - missing  : challenge biến mất khỏi API → giữ nguyên local + đánh dấu
               removed_from_server=true
  - refresh-meta: attachment thiếu trên đĩa → re-download khi --refresh-meta,
               KHÔNG re-download khi chỉ --update; guard idempotent (status/
               submitted_flag/README user) phải sống sót qua cả 2 đường.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

from ctf_downloader.config import DownloaderConfig
from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.services import pull_service
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

class FakePlatform:
    """Platform giả: fetch_challenges trả danh sách tĩnh, hỗ trợ
    fetch_solve_attribution qua attr_map."""

    platform_type = "generic"

    def __init__(self, challenges, title="IncCTF", url="https://inc.example.com",
                 attr_map=None):
        self.ctf_info = CTFInfo(title=title, url=url,
                                platform_type=self.platform_type)
        self.ctf_info.challenges = list(challenges)
        self._challenges = list(challenges)
        self.attr_map = dict(attr_map or {})
        self.attribution_calls = []

    def authenticate(self):
        return True

    def fetch_challenges(self):
        return list(self._challenges)

    def fetch_solve_attribution(self, ids):
        self.attribution_calls.append(list(ids))
        return {i: self.attr_map.get(i) for i in ids}


class DownloadCounter(pull_service.DownloadManager):
    """Spy đếm số lần thực sự tải attachment và GIẢ LẬP tải thành công:
    ghi 1 file giả cho mỗi entry trong ``files`` (không gọi mạng)."""
    download_calls = []

    def download_challenge_files(self, *args, **kwargs):
        files = args[0] if args else kwargs.get("files")
        dest_dir = args[2] if len(args) > 2 else kwargs.get("dest_dir")
        extracted = args[1] if len(args) > 1 else kwargs.get("extracted_links")
        third_party = (args[3] if len(args) > 3
                       else kwargs.get("download_third_party", True))
        type(self).download_calls.append(files)
        results = []
        for url, name in (files or []):
            os.makedirs(dest_dir, exist_ok=True)
            path = os.path.join(dest_dir, name)
            with open(path, "wb") as f:
                f.write(b"fake-download-bytes")
            results.append({"url": url, "name": name, "saved_path": path,
                            "success": True, "source": "platform_attachment"})
        # Không mô phỏng third-party link — các challenge test không có link.
        del extracted, third_party
        return results


def make_chall(cid, name, category, points=100, **kw):
    kw.setdefault("description", f"Plain description of {name}. No links here.")
    kw.setdefault("files", [])
    return Challenge(id=cid, name=name, category=category, points=points, **kw)


def detect_patch(platform):
    return patch.object(pull_service.PlatformDetector, "detect_platform",
                        return_value=platform)


def dm_patch():
    return patch.object(pull_service, "DownloadManager", DownloadCounter)


class IncrementalPullBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="inc_pull_")
        self.out_dir = os.path.join(self._tmp, "ws")
        DownloadCounter.download_calls = []
        self.config = DownloaderConfig(
            url="https://inc.example.com",
            cookie="session=abc",
            output_dir=self.out_dir,
            threads=2,
        )
        # Round 1 — full pull nền móng: Alpha(1), Beta(2), Epsilon(4), Zeta(5)
        self.round1 = FakePlatform([
            make_chall(1, "Alpha", "Web", 100),
            make_chall(2, "Beta", "Pwn", 200),
            make_chall(4, "Epsilon", "Crypto", 150),
            make_chall(5, "Zeta", "Misc", 50),
        ])
        with detect_patch(self.round1), dm_patch():
            result = PullService.run(self.config)
        self.assertTrue(result["ok"], "round-1 full pull phải thành công")
        self.repo = WorkspaceRepo(self.out_dir)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def meta_path_of(self, cid):
        for mp in self.repo.iter_challenges():
            m = self.repo.read_metadata(mp)
            if str(m.get("id")) == str(cid):
                return mp
        self.fail(f"không tìm thấy metadata cho id={cid}")

    def readme_of(self, cid):
        mp = self.meta_path_of(cid)
        return os.path.join(os.path.dirname(mp), "writeup", "README.md")


class TestIncrementalUpdate(IncrementalPullBase):
    """--update: new + updated + missing trong một kịch bản giải dài."""

    def test_new_updated_missing(self):
        # User state trước round 2: Beta đã solved + có notes + flag trong README
        beta_mp = self.meta_path_of(2)
        self.repo.update_status(beta_mp, lambda st: {
            **st, "solve": "solved_by_me", "notes": "my private note"})
        with open(self.readme_of(2), "w", encoding="utf-8") as f:
            f.write("# Beta writeup\n\nFlag: `FLAG{user_wrote_this}`\n")

        # Round 2 — --update:
        #   Beta(2) đổi points + connection_info  → updated
        #   Epsilon(4) không đổi metadata nhưng server báo by_team → updated
        #   Zeta(5) y nguyên                       → skipped
        #   Gamma(3) mới                          → new (full pipeline)
        #   Alpha(1) biến mất                     → missing (giữ local)
        round2 = FakePlatform([
            make_chall(2, "Beta", "Pwn", 250,
                       connection_info="nc chal.example.com 1337"),
            make_chall(3, "Gamma", "Web", 300),
            make_chall(4, "Epsilon", "Crypto", 150),
            make_chall(5, "Zeta", "Misc", 50),
        ], attr_map={4: {"by_me": False, "by_team": True}})
        cfg = DownloaderConfig(
            url="https://inc.example.com", cookie="session=abc",
            output_dir=self.out_dir, threads=2, incremental_update=True)

        n_calls_before = len(DownloadCounter.download_calls)
        with detect_patch(round2), dm_patch():
            result = PullService.run_update(cfg)

        # Counters
        self.assertTrue(result["ok"])
        self.assertEqual((result["new"], result["updated"],
                          result["skipped"], result["missing"]),
                         (1, 2, 1, 1), result)

        # Challenge MỚI được xử lý đầy đủ (workspace dựng chuẩn)
        gamma_mp = self.meta_path_of(3)
        gamma_dir = os.path.dirname(gamma_mp)
        for sub in ("challenge", "script", "solver", "writeup"):
            self.assertTrue(os.path.isdir(os.path.join(gamma_dir, sub)))
        self.assertTrue(os.path.isfile(os.path.join(gamma_dir, "challenge", "README.md")))

        # Challenge ĐÃ CÓ: đúng 1 lần tải attachment duy nhất (cho Gamma);
        # Beta/Epsilon/Zeta KHÔNG bị tải lại.
        self.assertEqual(len(DownloadCounter.download_calls), n_calls_before + 1)

        # Metadata động của Beta được cập nhật...
        beta_meta = self.repo.read_metadata(beta_mp)
        self.assertEqual(beta_meta.get("points"), 250)
        self.assertEqual(beta_meta.get("connection_info"), "nc chal.example.com 1337")

        # ... nhưng guard idempotent: status/flag/README user GIỮ NGUYÊN
        beta_status = self.repo.read_status(beta_mp)
        self.assertEqual(beta_status["solve"], "solved_by_me")
        self.assertEqual(beta_status["notes"], "my private note")
        with open(self.readme_of(2), encoding="utf-8") as f:
            self.assertIn("FLAG{user_wrote_this}", f.read())

        # Solved state raise-only qua fetch_solve_attribution:
        eps_status = self.repo.read_status(self.meta_path_of(4))
        self.assertEqual(eps_status["solve"], "solved_by_team")
        self.assertIsNotNone(eps_status["synced_at"])
        # Beta đã solved_by_me — attribution rỗng không được HẠ trạng thái
        self.assertEqual(self.repo.read_status(beta_mp)["solve"], "solved_by_me")

        # Challenge MISSING: giữ nguyên local + đánh dấu removed_from_server
        alpha_mp = self.meta_path_of(1)
        alpha_dir = os.path.dirname(alpha_mp)
        self.assertTrue(os.path.isdir(alpha_dir))
        alpha_meta = self.repo.read_metadata(alpha_mp)
        self.assertIs(alpha_meta.get("removed_from_server"), True)
        self.assertEqual((alpha_meta.get("status") or {}).get("removed_from_server"),
                         True)

        # Summary chạy lại: phản ánh danh sách mới (Gamma có mặt, tổng điểm cập nhật)
        with open(os.path.join(self.out_dir, "SUMMARY.md"), encoding="utf-8") as f:
            summary = f.read()
        self.assertIn("Gamma", summary)
        self.assertIn("Beta", summary)
        cj = json.load(open(os.path.join(self.out_dir, "challenges.json"),
                            encoding="utf-8"))
        api_ids = {str(c["id"]) for c in cj.get("challenges", [])}
        self.assertEqual(api_ids, {"2", "3", "4", "5"})
        beta_entry = next(c for c in cj["challenges"] if str(c["id"]) == "2")
        self.assertEqual(beta_entry.get("points"), 250)


class TestRefreshMeta(IncrementalPullBase):
    """--refresh-meta vs --update khi attachment thiếu trên đĩa."""

    def _give_alpha_attachment(self):
        """Giả lập Alpha từng tải 1 file (metadata + file thật trên đĩa)."""
        alpha_mp = self.meta_path_of(1)
        alpha_dir = os.path.dirname(alpha_mp)
        saved = os.path.join(alpha_dir, "challenge", "attach.zip")
        os.makedirs(os.path.dirname(saved), exist_ok=True)
        with open(saved, "wb") as f:
            f.write(b"PK\x03\x04fake")

        def _mut(meta):
            meta["downloaded_files"] = [{
                "url": "https://inc.example.com/files/attach.zip",
                "name": "attach.zip", "saved_path": saved,
                "success": True, "source": "platform_attachment"}]
            return meta
        self.repo.update_metadata(alpha_mp, _mut)
        return alpha_mp, saved

    def _protect_alpha_user_state(self):
        alpha_mp = self.meta_path_of(1)
        self.repo.update_status(alpha_mp, lambda st: {
            **st, "notes": "alpha user note"})
        with open(self.readme_of(1), "w", encoding="utf-8") as f:
            f.write("# Alpha writeup\n\nFlag: `FLAG{alpha_flag}`\n")
        return alpha_mp

    def test_update_does_not_redownload_missing_file_but_refresh_meta_does(self):
        alpha_mp, saved = self._give_alpha_attachment()
        self._protect_alpha_user_state()
        os.remove(saved)   # file biến mất trên đĩa

        round2 = FakePlatform([make_chall(1, "Alpha", "Web", 100)])
        base = dict(url="https://inc.example.com", cookie="session=abc",
                    output_dir=self.out_dir, threads=1)

        # --update: KHÔNG tải lại dù file thiếu (metadata không đổi → skipped)
        n_before = len(DownloadCounter.download_calls)
        with detect_patch(round2), dm_patch():
            r1 = PullService.run_update(
                DownloaderConfig(incremental_update=True, **base))
        self.assertEqual(r1["updated"], 0)
        self.assertEqual(r1["skipped"], 1)
        self.assertEqual(len(DownloadCounter.download_calls), n_before,
                         "--update không được tải lại attachment")

        # --refresh-meta: có tải lại (API giờ trả Alpha kèm attachment)
        round2_attach = FakePlatform([make_chall(
            1, "Alpha", "Web", 100,
            files=[("https://inc.example.com/files/attach.zip", "attach.zip")])])
        with detect_patch(round2_attach), dm_patch():
            r2 = PullService.run_update(
                DownloaderConfig(refresh_meta=True, **base))
        self.assertEqual(r2["updated"], 1)
        self.assertEqual(len(DownloadCounter.download_calls), n_before + 1,
                         "--refresh-meta phải tải lại attachment thiếu")
        # File được khôi phục vào challenge/
        self.assertTrue(os.path.isfile(saved))

        # Guard: status + README user vẫn sống sót qua builder viết lại metadata
        st = self.repo.read_status(alpha_mp)
        self.assertEqual(st["notes"], "alpha user note")
        with open(self.readme_of(1), encoding="utf-8") as f:
            self.assertIn("FLAG{alpha_flag}", f.read())
        # removed flag không bị mark oan (Alpha vẫn còn trên API)
        meta = self.repo.read_metadata(alpha_mp)
        self.assertNotIn("removed_from_server", meta)
        self.assertNotIn("removed_from_server", meta.get("status") or {})

    def test_refresh_meta_skips_when_all_files_present(self):
        self._give_alpha_attachment()   # file còn nguyên trên đĩa
        round2 = FakePlatform([make_chall(1, "Alpha", "Web", 100)])
        n_before = len(DownloadCounter.download_calls)
        with detect_patch(round2), dm_patch():
            r = PullService.run_update(DownloaderConfig(
                refresh_meta=True, url="https://inc.example.com",
                cookie="session=abc", output_dir=self.out_dir, threads=1))
        self.assertEqual(len(DownloadCounter.download_calls), n_before,
                         "file đủ trên đĩa thì --refresh-meta cũng không tải lại")
        self.assertEqual(r["skipped"], 1)


class TestCliFlagsAndDefaults(unittest.TestCase):
    """CLI parse cờ mới + mặc định vẫn full pull."""

    def test_parser_accepts_update_flags(self):
        from ctf_downloader.cli import build_unified_parser
        p = build_unified_parser()
        args = p.parse_args(["pull", "-u", "https://x.io", "--update"])
        self.assertTrue(args.update)
        self.assertFalse(args.refresh_meta)
        args = p.parse_args(["pull", "-u", "https://x.io", "--refresh-meta"])
        self.assertFalse(args.update)
        self.assertTrue(args.refresh_meta)
        args = p.parse_args(["pull", "-u", "https://x.io"])
        self.assertFalse(args.update)
        self.assertFalse(args.refresh_meta)

    def test_default_config_not_incremental(self):
        cfg = DownloaderConfig(url="https://x.io")
        self.assertFalse(cfg.incremental_update)
        self.assertFalse(cfg.refresh_meta)


if __name__ == "__main__":
    unittest.main()
