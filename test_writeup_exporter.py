"""
WriteupExporter (P2-3) — unit tests trên tmp workspace giả lập.

Chạy: python3 -m pytest test_writeup_exporter.py -q
Không gọi mạng — toàn bộ dữ liệu được dựng trên đĩa trong tempfile.
"""
import contextlib
import io
import json
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path

from ctf_downloader.services.writeup_exporter import SOLVED_EXPORT_VALUES, WriteupExporter
from ctf_downloader.ui.banner import banner_b


def _make_workspace(root: Path) -> None:
    """Workspace giả lập 4 challenge:

    - Web/Easy_SQLi : solved_by_me + writeup complete (có flag + solver) → DUYỆT
    - Pwn/Hard_Heap : solved_by_team + writeup draft (KHÔNG flag)      → DUYỆT + warning
    - Crypto/RSA_1  : solved_by_me nhưng writeup = none               → LOẠI
    - Forensics/Zip : writeup complete nhưng chưa solve                → LOẠI
    """
    (root / "challenges.json").write_text(json.dumps({
        "ctf_info": {
            "title": "Vòng loại PTIT CTF 2026",
            "url": "https://jeo.infosecptit.org",
            "platform": "gzctf",
            "user": "B23DCCE070",
        },
    }, ensure_ascii=False), encoding="utf-8")

    def make_chal(cat: str, name: str, points: int, solve: str,
                  writeup_state: str, writeup_text: str, solver: bool):
        chal_dir = root / cat / name
        (chal_dir / "writeup").mkdir(parents=True)
        (chal_dir / "metadata.json").write_text(json.dumps({
            "id": hash((cat, name)) % 10000,
            "name": name.replace("_", " "),
            "category": cat,
            "points": points,
            "status": {
                "schema_version": 2,
                "solve": solve,
                "flag": {"value": None, "state": "none"},
                "writeup": writeup_state,
                "writeup_auto": True,
            },
        }, ensure_ascii=False), encoding="utf-8")
        (chal_dir / "writeup" / "README.md").write_text(writeup_text, encoding="utf-8")
        if solver:
            (chal_dir / "solver").mkdir()
            (chal_dir / "solver" / "solve.py").write_text(
                "print('pwned')\n", encoding="utf-8")

    make_chal(
        "Web", "Easy_SQLi", 100, "solved_by_me", "complete",
        "# Writeup\n\nFlag lấy được: `PTIT{sql1_1s_fun}`\n\n```python\npayload = \"' OR 1=1--\"\n```\n",
        solver=True,
    )
    make_chal(
        "Pwn", "Hard_Heap", 500, "solved_by_team", "draft",
        "# Writeup heap\n\nChưa ghi flag lại (placeholder FLAG{...}).\n",
        solver=False,
    )
    make_chal("Crypto", "RSA_1", 200, "solved_by_me", "none", "# trống\n", solver=True)
    make_chal("Forensics", "Zip_Bomb", 150, "unsolved", "complete",
              "Flag `PTIT{nope}` nhưng chưa solve.\n", solver=False)


class TestWriteupExporter(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="ws_writeup_test_")
        self.ws = Path(self._tmp) / "PTIT_CTF_2026"
        self.ws.mkdir()
        _make_workspace(self.ws)

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_collect_filters_solved_with_writeup(self):
        entries = WriteupExporter(self.ws).collect()
        names = sorted(e.name for e in entries)
        # Chỉ Easy SQLi (solved_by_me) và Hard Heap (solved_by_team) đạt điều kiện
        self.assertEqual(names, ["Easy SQLi", "Hard Heap"])

    def test_collect_entry_fields(self):
        exporter = WriteupExporter(self.ws)
        entries = {e.name: e for e in exporter.collect()}

        easy = entries["Easy SQLi"]
        self.assertEqual(easy.category, "Web")
        self.assertEqual(easy.points, 100)
        self.assertEqual(easy.flag, "PTIT{sql1_1s_fun}")
        self.assertEqual([p.name for p in easy.solver_files], ["solve.py"])
        self.assertIn("sql1_1s_fun", easy.writeup_md)

        hard = entries["Hard Heap"]
        self.assertIsNone(hard.flag)  # placeholder FLAG{...} không tính là flag thật
        self.assertEqual(hard.solver_files, [])

    def test_validate_warns_missing_flag_only(self):
        exporter = WriteupExporter(self.ws)
        entries = exporter.collect()
        warnings = exporter.validate(entries)
        self.assertEqual(len(warnings), 1)
        self.assertIn("Hard Heap", warnings[0])

    def test_build_pack_structure_and_zip(self):
        out_dir = Path(self._tmp) / "out"
        pack_dir = WriteupExporter(self.ws).build_pack(out_dir=out_dir)

        self.assertTrue(pack_dir.is_dir())
        index = (pack_dir / "INDEX.md").read_text(encoding="utf-8")
        # Banner half-block PHOSPHOR (text thuần trong code fence) đứng
        # trước heading chính.
        banner_lines = [ln for ln in banner_b().plain.rstrip("\n").splitlines()
                        if ln.strip()]
        self.assertTrue(banner_lines)
        for ln in banner_lines:
            self.assertIn(ln, index)
        self.assertIn("```text", index)
        self.assertLess(index.index("```text"), index.index("# Writeup Pack"))
        # Header từ challenges.json ctf_info
        self.assertIn("# Writeup Pack — Vòng loại PTIT CTF 2026", index)
        self.assertIn("B23DCCE070", index)
        # Bảng tổng hợp 6 cột, căn cột markdown chuẩn
        self.assertIn("| # | Category | Challenge | Points | Flag | Solver |", index)
        self.assertIn("| ---: | :--- | :--- | ---: | :--- | :---: |", index)
        # Thứ tự dòng theo thứ tự quét workspace — assert không phụ thuộc #
        self.assertIn("| Web | Easy SQLi | 100 | `PTIT{sql1_1s_fun}` | ✔ |", index)
        self.assertIn("| Pwn | Hard Heap | 500 | `N/A` | — |", index)
        self.assertNotIn("✅", index)  # emoji thay bằng glyph PHOSPHOR
        # Không còn cột Link riêng — link tương đối nằm ở mục Chi tiết
        self.assertNotIn("| Link |", index)
        self.assertIn("[Web_Easy_SQLi/README.md](Web_Easy_SQLi/README.md)", index)
        self.assertIn("Pwn_Hard_Heap/README.md", index)
        # Không lẫn bài bị loại
        self.assertNotIn("RSA_1", index)
        self.assertNotIn("Zip_Bomb", index)
        # Cảnh báo thiếu flag nằm trong INDEX với glyph ``!`` (không emoji)
        self.assertIn("## ! Cảnh báo validate", index)
        self.assertIn("- ! [Pwn] Hard Heap: không tìm thấy flag thật", index)
        self.assertNotIn("⚠️", index)

        # Per-challenge: writeup gốc + solver copy kèm
        web_readme = (pack_dir / "Web_Easy_SQLi" / "README.md").read_text(encoding="utf-8")
        self.assertIn("PTIT{sql1_1s_fun}", web_readme)
        solver_py = pack_dir / "Web_Easy_SQLi" / "solver" / "solve.py"
        self.assertTrue(solver_py.is_file())
        self.assertEqual(solver_py.read_text(encoding="utf-8"), "print('pwned')\n")
        # Bài thiếu solver không tạo thư mục solver rác
        self.assertFalse((pack_dir / "Pwn_Hard_Heap" / "solver").exists())

        # Zip tồn tại và chứa INDEX.md
        zip_path = Path(str(pack_dir) + ".zip")
        self.assertTrue(zip_path.is_file())
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            self.assertIn(f"{pack_dir.name}/INDEX.md", names)
            self.assertIn(f"{pack_dir.name}/Web_Easy_SQLi/README.md", names)

    def test_build_pack_console_output_phosphor(self):
        """build_pack in cảnh báo ``!`` warn + tổng kết ``✔ Đã đóng gói N
        bài → <path>`` ra stderr (err_console), không dùng emoji."""
        err_buf = io.StringIO()
        with contextlib.redirect_stderr(err_buf):
            pack_dir = WriteupExporter(self.ws).build_pack(
                out_dir=Path(self._tmp) / "out_console")
        err = err_buf.getvalue()
        # Tổng kết: ✔ glyph + count + path
        self.assertIn("✔ Đã đóng gói 2 bài → ", err)
        self.assertIn(str(pack_dir), err)
        # Cảnh báo validate: glyph ``!`` thay emoji ⚠️
        self.assertIn("! [Pwn] Hard Heap: không tìm thấy flag thật", err)
        self.assertNotIn("⚠️", err)

    def test_build_pack_empty_raises_valueerror(self):
        empty_ws = Path(self._tmp) / "Empty_CTF"
        empty_ws.mkdir()
        (empty_ws / "challenges.json").write_text("{}", encoding="utf-8")
        with self.assertRaises(ValueError) as ctx:
            WriteupExporter(empty_ws).build_pack(out_dir=self._tmp)
        msg = str(ctx.exception)
        # Thông điệp hướng dẫn cách khắc phục
        for hint in ("solve", "writeup"):
            self.assertIn(hint, msg)
        self.assertIn(str(SOLVED_EXPORT_VALUES), msg)


if __name__ == "__main__":
    unittest.main()
