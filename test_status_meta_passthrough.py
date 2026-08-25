"""PERF regression — metadata.json KHÔNG được đọc đôi trong đường scan/render.

perf-audit (/tmp/perf_audit/RESULTS.md): status_service.compute_status gọi
``repo.read_status(meta_path)`` không truyền ``meta`` dù caller đã đọc
metadata → mỗi challenge bị parse metadata.json 2 lần (~-85% render time khi
bỏ). Test này khoá hành vi: số lượt đọc metadata phải đúng O(số-challenge),
và output status/render KHÔNG đổi so với đường tham chiếu.

Chạy: python3 -m pytest test_status_meta_passthrough.py -q
"""
import io
import json
import pathlib
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout

from ctf_downloader.services.status_service import StatusService
from ctf_downloader.storage.workspace_repo import WorkspaceRepo

N_CHALLS = 4


class CountingRepo(WorkspaceRepo):
    """WorkspaceRepo thật + bộ đếm lượt read_metadata (spy perf)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.metadata_reads = 0

    def read_metadata(self, path):
        self.metadata_reads += 1
        return super().read_metadata(path)


def make_multi_workspace(root: pathlib.Path, n: int = N_CHALLS):
    """Workspace n challenge: 2 solved (marker tick), 1 working, còn lại
    unsolved; đủ writeup template để assessor chạy cùng đường như prod."""
    cats = ["Web", "Crypto", "Pwn", "Misc"]
    for i in range(1, n + 1):
        cat = cats[i % len(cats)]
        d = root / cat / f"chall_{i}"
        d.mkdir(parents=True)
        (root / "challenges.json").write_text(json.dumps({
            "ctf_info": {"title": "MetaPassCTF", "url": "https://m.example.com",
                         "platform": "gzctf"},
            "challenges": [],
        }), encoding="utf-8")
        solved = i <= 2
        meta = {
            "id": i, "name": f"Chall {i}", "category": cat,
            "points": 100 * i, "solved_by_me": solved,
            "description": f"Desc {i}",
        }
        (d / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")
        wu = d / "writeup"
        wu.mkdir()
        marker = "- [x] Solved" if solved else "- [ ] Solved"
        (wu / "README.md").write_text(
            f"# Writeup — Chall {i}\n\n{marker}\n\n"
            "**Flag**: `FLAG{PLACEHOLDER}`\n", encoding="utf-8")


class MetaPassthroughCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="meta_pass_")
        self.root = pathlib.Path(self._tmp) / "ws"
        self.root.mkdir()
        make_multi_workspace(self.root)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _meta_paths(self):
        return sorted(self.root.rglob("metadata.json"))


class TestComputeStatusNoReread(MetaPassthroughCase):
    def test_compute_status_with_meta_zero_extra_read(self):
        repo = CountingRepo(self.root)
        mp = self._meta_paths()[0]
        meta = repo.read_metadata(mp)
        baseline = repo.metadata_reads  # 1 lần của caller
        st = StatusService.compute_status(repo, mp, meta=meta)
        self.assertEqual(
            repo.metadata_reads, baseline,
            "compute_status(meta=...) phải TÁI SỬ DỤNG meta, không đọc lại "
            "metadata.json qua read_status")
        # Hành vi output KHÔNG đổi: khớp nguyên đường tham chiếu
        # compute_status đọc-1-lần (gồm cả trục writeup đã assess).
        ref = StatusService.compute_status(WorkspaceRepo(self.root), mp)
        self.assertEqual(st, ref)

    def test_compute_status_without_meta_reads_exactly_once(self):
        repo = CountingRepo(self.root)
        mp = self._meta_paths()[0]
        StatusService.compute_status(repo, mp)   # interface tối thiểu
        self.assertEqual(repo.metadata_reads, 1)


class TestScanOncePerChallenge(MetaPassthroughCase):
    def test_scan_reads_metadata_once_per_challenge(self):
        repo = CountingRepo(self.root)
        results = StatusService.scan_local_challenges(repo)
        self.assertEqual(len(results), N_CHALLS)
        self.assertEqual(
            repo.metadata_reads, N_CHALLS,
            f"scan phải đọc metadata đúng 1 lần/chall "
            f"(={N_CHALLS}), nhận {repo.metadata_reads} (double-read?)")

    def test_scan_output_unchanged(self):
        repo = CountingRepo(self.root)
        results = StatusService.scan_local_challenges(repo)
        by_id = {c["id"]: c for c in results}
        # Trục solve phản ánh đúng đĩa (2 chall đầu solved qua marker).
        self.assertTrue(by_id[1]["solved_by_me"])
        self.assertTrue(by_id[2]["solved_by_me"])
        self.assertFalse(by_id[3]["solved_by_me"])
        self.assertEqual(by_id[1]["_status"]["solve"], "solved_by_me")
        self.assertEqual(by_id[3]["_status"]["solve"], "unsolved")
        # Khớp nguyên đường tham chiếu compute_status từng chall
        # (đọc-1-lần, gồm trục writeup đã assess).
        real = WorkspaceRepo(self.root)
        for mp in self._meta_paths():
            mid = json.loads(mp.read_text(encoding="utf-8"))["id"]
            self.assertEqual(by_id[mid]["_status"],
                             StatusService.compute_status(real, mp))


class TestRenderTreeOutputUnchanged(MetaPassthroughCase):
    def test_render_tree_no_reread_and_same_rows(self):
        repo = CountingRepo(self.root)
        stats = StatusService.summary_stats(repo)
        after_setup = repo.metadata_reads

        buf = io.StringIO()
        with redirect_stdout(buf):
            StatusService.render_tree(repo, stats=stats)
        out = buf.getvalue()

        # Render từ stats có sẵn KHÔNG đọc thêm metadata nào.
        self.assertEqual(
            repo.metadata_reads, after_setup,
            "render_tree(stats=...) không được đọc lại metadata.json")
        # Hành vi output: đủ 4 chall, tên xuất hiện, đếm solved đúng (header).
        for i in range(1, N_CHALLS + 1):
            self.assertIn(f"Chall {i}", out)
        self.assertIn("2/4", out)


if __name__ == "__main__":
    unittest.main()
