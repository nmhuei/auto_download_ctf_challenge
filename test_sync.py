"""P2-1: sync_workspace 2 chiều + verify drift — unit tests.

Chạy: python3 -m pytest test_sync.py -q
Toàn bộ platform được mock — KHÔNG gọi mạng, KHÔNG đụng DownloadManager
(sync_workspace nhận (repo, platform) trực tiếp nên không cần patch detector).

Phủ 3 acceptance criteria:
  - merge metadata động giữ nguyên status block + solver/ + writeup/ + challenge/
    (chỉ points/solves_count/connection_info đổi; stamp synced_at).
  - challenge mới trên server → chỉ liệt kê new_on_server, không tạo gì.
  - verify phát hiện drift đúng: server báo solved mà local chưa → liệt kê
    kèm solver_names, KHÔNG tự đổi trạng thái local.
"""
import hashlib
import os
import shutil
import tempfile
import unittest

from ctf_downloader.models import Challenge, CTFInfo
from ctf_downloader.platforms.base import SolveAttribution
from ctf_downloader.services.pull_service import PullService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo


# ----------------------------------------------------------------------
# Helpers — workspace fixture dựng thủ công (không cần full pull)
# ----------------------------------------------------------------------

SEED_CHALLENGES = [
    (1, "Alpha", "Web", 100),
    (2, "Beta", "Pwn", 200),
    (4, "Epsilon", "Crypto", 150),
]


def make_chall(cid, name, category, points=100, **kw):
    kw.setdefault("description", f"Plain description of {name}. No links.")
    kw.setdefault("files", [])
    return Challenge(id=cid, name=name, category=category, points=points, **kw)


def build_workspace(root):
    """Dựng workspace giả tối thiểu: metadata.json + challenge/ + solver/ +
    writeup/ cho 3 challenge nền móng. Trả về WorkspaceRepo."""
    repo = WorkspaceRepo(root)
    for cid, name, cat, pts in SEED_CHALLENGES:
        d = os.path.join(root, cat.lower(), name.lower())
        for sub in ("challenge", "solver", "writeup"):
            os.makedirs(os.path.join(d, sub), exist_ok=True)
        repo.write_metadata(os.path.join(d, "metadata.json"),
                            {"id": cid, "name": name, "category": cat,
                             "points": pts})
        with open(os.path.join(d, "challenge", "README.md"), "w",
                  encoding="utf-8") as f:
            f.write(f"# {name} description\n")
        with open(os.path.join(d, "solver", "solve.py"), "w",
                  encoding="utf-8") as f:
            f.write(f"# solve script {name}\n")
        with open(os.path.join(d, "writeup", "README.md"), "w",
                  encoding="utf-8") as f:
            f.write(f"# writeup {name}\n")
    return repo


def file_digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def snapshot_user_files(root):
    """Hash mọi file trong solver/ và writeup/ của toàn bộ challenge."""
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if os.path.basename(dirpath) in ("solver", "writeup"):
            for fn in files:
                p = os.path.join(dirpath, fn)
                snap[os.path.join(rel, fn)] = file_digest(p)
    return snap


class FakePlatform:
    """Platform giả cho sync_workspace: fetch_challenges tĩnh +
    fetch_solve_attribution qua attr_map (giá trị dict hoặc SolveAttribution)."""

    platform_type = "generic"

    def __init__(self, challenges, title="SyncCTF",
                 url="https://sync.example.com", attr_map=None):
        self.ctf_info = CTFInfo(title=title, url=url,
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


class SyncTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="sync_ws_")
        self.out_dir = os.path.join(self._tmp, "ws")
        self.repo = build_workspace(self.out_dir)
        # User state: Beta solved_by_me + notes + flag trong README writeup;
        # Epsilon để nguyên (unsolved).
        beta_mp = self.meta_path_of(2)
        self.repo.update_status(beta_mp, lambda st: {
            **st, "solve": "solved_by_me", "notes": "my private note"})
        self.beta_readme = os.path.join(
            os.path.dirname(beta_mp), "writeup", "README.md")
        with open(self.beta_readme, "w", encoding="utf-8") as f:
            f.write("# Beta writeup\n\nFlag: `FLAG{user_wrote_this}`\n")

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def meta_path_of(self, cid):
        for mp in self.repo.iter_challenges():
            m = self.repo.read_metadata(mp)
            if str(m.get("id")) == str(cid):
                return mp
        self.fail(f"không tìm thấy metadata cho id={cid}")


class TestSyncMerge(SyncTestBase):
    """Merge metadata động giữ local state; challenge mới không bị tạo."""

    def test_merge_preserves_local_state_and_lists_new(self):
        # Round sync: Beta đổi points + connection_info; Epsilon y nguyên;
        # Gamma(3) mới trên server. Không ai solved trên server.
        plat = FakePlatform([
            make_chall(1, "Alpha", "Web", 100),
            make_chall(2, "Beta", "Pwn", 250,
                       connection_info="nc chal.example.com 1337",
                       solves_count=42),
            make_chall(4, "Epsilon", "Crypto", 150),
            make_chall(3, "Gamma", "Web", 300),
        ])

        user_files_before = snapshot_user_files(self.out_dir)
        result = PullService.sync_workspace(self.repo, plat)

        self.assertTrue(result["ok"])
        # updated=N · new=X · drift=Y
        self.assertEqual(result["updated"], 1)   # chỉ Beta đổi metadata động
        self.assertEqual(result["new"], 1)
        self.assertEqual(result["drift"], [])

        # Challenge MỚI: chỉ liệt kê — KHÔNG tạo thư mục/workspace nào
        gamma_dirs = [os.path.join(self.out_dir, d)
                      for d in os.listdir(self.out_dir)
                      if "gamma" in d.lower()]
        self.assertEqual(gamma_dirs, [])
        self.assertFalse(os.path.isdir(
            os.path.join(self.out_dir, "web", "Gamma")))
        self.assertEqual([c["name"] for c in result["new_on_server"]], ["Gamma"])
        self.assertEqual([c["id"] for c in result["new_on_server"]], [3])

        # Metadata ĐỘNG của Beta được cập nhật...
        beta_mp = self.meta_path_of(2)
        beta_meta = self.repo.read_metadata(beta_mp)
        self.assertEqual(beta_meta.get("points"), 250)
        self.assertEqual(beta_meta.get("connection_info"),
                         "nc chal.example.com 1337")
        self.assertEqual(beta_meta.get("solves_count"), 42)

        # ... nhưng status block GIỮ NGUYÊN (trừ synced_at mới được stamp)
        beta_status = self.repo.read_status(beta_mp)
        self.assertEqual(beta_status["solve"], "solved_by_me")
        self.assertEqual(beta_status["notes"], "my private note")
        self.assertIsNotNone(beta_status.get("synced_at"))

        # submitted_flag / README / solver / challenge: không file nào đụng tới
        with open(self.beta_readme, encoding="utf-8") as f:
            self.assertIn("FLAG{user_wrote_this}", f.read())
        self.assertEqual(snapshot_user_files(self.out_dir), user_files_before)
        beta_dir = os.path.dirname(beta_mp)
        for sub in ("challenge", "solver", "writeup"):
            self.assertTrue(os.path.isdir(os.path.join(beta_dir, sub)))
        self.assertTrue(os.path.isfile(
            os.path.join(beta_dir, "challenge", "README.md")))

        # Idempotent: sync lần nữa (server y nguyên) → updated=0
        result2 = PullService.sync_workspace(self.repo, plat)
        self.assertEqual(result2["updated"], 0)


class TestSyncVerifyDrift(SyncTestBase):
    """verify(): server báo solved mà local chưa → drift kèm solver_names."""

    def test_verify_detects_drift_with_solver_names_and_never_autofixes(self):
        # Epsilon(4): server báo TEAM đã giải kèm tên người giải — local chưa.
        # Beta(2): local đã solved_by_me → dù server báo by_team vẫn KHÔNG drift.
        plat = FakePlatform(
            [make_chall(cid, n, c, p) for cid, n, c, p in SEED_CHALLENGES],
            attr_map={
                4: SolveAttribution(by_team=True,
                                    solver_names=["alice", "bob"]),
                2: SolveAttribution(by_team=True, solver_names=["carol"]),
            })

        verdict = PullService.verify(self.repo, plat)
        self.assertTrue(verdict["ok"])
        self.assertEqual(verdict["checked"], 3)
        drift = verdict["unsolved_locally_solved_remotely"]
        self.assertEqual(len(drift), 1, verdict)
        eps = drift[0]
        self.assertEqual(str(eps["id"]), "4")
        self.assertEqual(eps["name"], "Epsilon")
        self.assertEqual(eps["category"], "Crypto")
        self.assertFalse(eps["by_me"])
        self.assertTrue(eps["by_team"])
        self.assertEqual(eps["solver_names"], ["alice", "bob"])
        self.assertEqual(eps["local_solve"], "unsolved")

        # sync_workspace tổng hợp drift vào bảng kết quả...
        result = PullService.sync_workspace(self.repo, plat)
        self.assertEqual(result["drift"], drift)
        self.assertEqual(len(result["drift"]), 1)

        # ... nhưng KHÔNG BAO GIỜ tự đổi trạng thái — user quyết.
        eps_status = self.repo.read_status(self.meta_path_of(4))
        self.assertEqual(eps_status["solve"], "unsolved")

    def test_verify_accepts_plain_dict_attr_and_no_attribution_platform(self):
        # attr dạng dict thuần (không phải dataclass) cũng xử lý được
        plat_dict = FakePlatform(
            [make_chall(cid, n, c, p) for cid, n, c, p in SEED_CHALLENGES],
            attr_map={1: {"by_me": True, "by_team": False,
                          "solver_names": ["dave"]}})
        drift = PullService.verify(self.repo, plat_dict)[
            "unsolved_locally_solved_remotely"]
        self.assertEqual([d["id"] for d in drift], [1])
        self.assertEqual(drift[0]["solver_names"], ["dave"])

        # Platform không hỗ trợ attribution → rỗng, không lỗi
        class NoAttrPlatform(FakePlatform):
            def fetch_solve_attribution(self, ids):  # noqa: D401
                raise NotImplementedError

        plat_none = NoAttrPlatform(
            [make_chall(cid, n, c, p) for cid, n, c, p in SEED_CHALLENGES])
        # NotImplementedError bị _fetch_attribution_map nuốt → drift rỗng
        v = PullService.verify(self.repo, plat_none)
        self.assertTrue(v["ok"])
        self.assertEqual(v["unsolved_locally_solved_remotely"], [])


if __name__ == "__main__":
    unittest.main()
