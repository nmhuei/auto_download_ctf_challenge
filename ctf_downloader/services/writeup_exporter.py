"""WriteupExporter — gom writeup các bài đã solve thành bộ chuẩn nộp BTC.

P2-3: quét workspace, lọc challenge có ``status.solve`` in
``("solved_by_me", "solved_by_team")`` VÀ ``status.writeup != "none"``,
rồi build một pack export:

    <out_dir>/<ws>_writeup_<YYYYMMDD>/
        INDEX.md                     # bảng tổng hợp + link tương đối từng bài
        <cat>_<name>/README.md       # nội dung writeup gốc
        <cat>_<name>/solver/...      # solver script kèm theo
    <out_dir>/<ws>_writeup_<YYYYMMDD>.zip   # zip cả pack

CÁCH GỌI SAU KHI WIRE CLI (cli.py đang bận — không wire ở đây):

    from ctf_downloader.services.writeup_exporter import WriteupExporter

    exporter = WriteupExporter(workspace_path="PTIT_CTF_2026")
    entries = exporter.collect()          # list[WriteupEntry] đã lọc
    warnings = exporter.validate(entries) # list[str] cảnh báo (thiếu flag...)
    pack_dir = exporter.build_pack(out_dir=".")  # trả Path thư mục pack

hoặc gộp: ``pack_dir = WriteupExporter(ws).build_pack(out_dir)`` — build_pack
tự gọi collect()/validate() bên trong.
"""
from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..storage.constants import FLAG_PLACEHOLDER
from ..storage.workspace_repo import WorkspaceRepo
from ..utils.sanitize import sanitize_folder_name

# Trục solve được tính là "đã giải bởi mình/team" — đủ điều kiện đưa vào pack.
SOLVED_EXPORT_VALUES = ("solved_by_me", "solved_by_team")

# Regex flag generic (giống writeup_assessor.GENERIC_FLAG_RE): PREFIX{body}
# với body đủ dài để loại nhiễu, không chứa {} hay newline.
_FLAG_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{2,24}\{[^{}\n]{4,256}\}")


@dataclass
class WriteupEntry:
    """Một challenge đạt điều kiện export."""

    name: str
    category: str
    points: object = None
    flag: Optional[str] = None
    solver_files: List[Path] = field(default_factory=list)
    writeup_md: str = ""
    # Nội bộ phục vụ build_pack:
    source_dir: Optional[Path] = None


class WriteupExporter:
    def __init__(self, workspace_path):
        self.root = Path(workspace_path)
        self.repo = WorkspaceRepo(self.root)

    # ------------------------------------------------------------------
    # collect
    # ------------------------------------------------------------------
    def collect(self) -> List[WriteupEntry]:
        """Quét metadata.json toàn workspace, lọc solved + có writeup.

        Dùng WorkspaceRepo.read_status (normalize + migrate-on-read) để hỗ trợ
        cả workspace schema mới (block ``status``) lẫn layout phẳng cũ
        (``solved_by_me`` legacy). Writeup đọc theo thứ tự
        ``<chal>/writeup/README.md`` → fallback ``<chal>/README.md``.
        """
        entries: List[WriteupEntry] = []
        for meta_path in self.repo.iter_challenges():
            try:
                status = self.repo.read_status(meta_path)
            except Exception:
                continue
            if status.get("solve") not in SOLVED_EXPORT_VALUES:
                continue
            if status.get("writeup", "none") == "none":
                continue

            chal_dir = meta_path.parent
            meta = self.repo.read_metadata(meta_path) or {}
            text = self._read_writeup_text(chal_dir)
            entry = WriteupEntry(
                name=str(meta.get("name") or chal_dir.name),
                category=str(meta.get("category") or "misc"),
                points=meta.get("points"),
                flag=self._extract_flag(text),
                solver_files=self._list_solver_files(chal_dir),
                writeup_md=text,
                source_dir=chal_dir,
            )
            entries.append(entry)
        return entries

    @staticmethod
    def _read_writeup_text(chal_dir: Path) -> str:
        for rel in ("writeup/README.md", "README.md"):
            p = chal_dir / rel
            if p.is_file():
                try:
                    return p.read_text(encoding="utf-8-sig")
                except Exception:
                    return ""
        return ""

    @staticmethod
    def _extract_flag(text: str) -> Optional[str]:
        """Flag thật đầu tiên trong writeup (bỏ placeholder FLAG{...} / ...)."""
        for m in _FLAG_RE.finditer(text or ""):
            cand = m.group(0)
            body = cand.split("{", 1)[1].rstrip("}").strip().lower()
            if not body or ".." in body:
                continue  # placeholder kiểu FLAG{...}
            return cand
        return None

    @staticmethod
    def _list_solver_files(chal_dir: Path) -> List[Path]:
        solver_dir = chal_dir / "solver"
        if not solver_dir.is_dir():
            return []
        return sorted(p for p in solver_dir.iterdir() if p.is_file())

    # ------------------------------------------------------------------
    # validate
    # ------------------------------------------------------------------
    def validate(self, entries: List[WriteupEntry]) -> List[str]:
        """Cảnh báo (không chặn build): bài thiếu flag thật trong writeup."""
        warnings: List[str] = []
        for e in entries:
            if not e.flag:
                warnings.append(
                    f"⚠️ [{e.category}] {e.name}: không tìm thấy flag thật "
                    f"trong writeup (placeholder {FLAG_PLACEHOLDER} chưa thay?)"
                )
            if not e.writeup_md.strip():
                warnings.append(
                    f"⚠️ [{e.category}] {e.name}: file writeup rỗng/không đọc được"
                )
        return warnings

    # ------------------------------------------------------------------
    # build_pack
    # ------------------------------------------------------------------
    def build_pack(self, out_dir=".") -> Path:
        """Build pack export + zip. Trả về Path thư mục pack.

        Raises:
            ValueError: không có challenge nào đạt điều kiện export —
                hướng dẫn user đánh dấu solve/writeup trước khi chạy.
        """
        entries = self.collect()
        if not entries:
            raise ValueError(
                "Không có challenge nào đạt điều kiện export writeup "
                f"(cần status.solve in {SOLVED_EXPORT_VALUES} và "
                "status.writeup != 'none'). Hãy đánh dấu solve + hoàn thiện "
                "writeup cho ít nhất một bài rồi chạy lại."
            )
        warnings = self.validate(entries)

        out_root = Path(out_dir)
        out_root.mkdir(parents=True, exist_ok=True)
        date_tag = _dt.date.today().strftime("%Y%m%d")
        ws_name = sanitize_folder_name(self.root.name)
        pack_dir = out_root / f"{ws_name}_writeup_{date_tag}"
        pack_dir.mkdir(parents=True, exist_ok=True)

        ctf_info = self._ctf_info()
        index_lines = self._render_index_header(ctf_info, len(entries))
        index_lines.extend(self._render_warnings(warnings))

        # Bảng tổng hợp
        index_lines.append("| # | Category | Challenge | Points | Flag | Solver | Link |")
        index_lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        rows = []
        for i, e in enumerate(entries, 1):
            sub = self._entry_dirname(e)
            self._export_entry(e, pack_dir / sub)
            link = f"{sub}/README.md"
            rows.append(
                f"| {i} | {e.category} | {e.name} | {e.points if e.points is not None else '-'} "
                f"| `{e.flag or 'N/A'}` | {'✅' if e.solver_files else '—'} | [{link}]({link}) |"
            )
        index_lines.extend(rows)

        index_lines.append("")
        index_lines.append("## 📂 Chi tiết từng bài")
        index_lines.append("")
        for i, e in enumerate(entries, 1):
            sub = self._entry_dirname(e)
            index_lines.append(f"{i}. **[{e.category}] {e.name}** — [{sub}/README.md]({sub}/README.md)")

        index_lines.append("")
        index_lines.append("---")
        index_lines.append(
            f"_Generated by WriteupExporter (P2-3) ngày "
            f"{_dt.date.today().isoformat()} — {len(entries)} challenge._"
        )

        (pack_dir / "INDEX.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")

        # Zip cả pack (bỏ chính file zip cũ nếu tồn tại từ lần chạy trước)
        zip_base = str(pack_dir)
        if os.path.exists(zip_base + ".zip"):
            os.remove(zip_base + ".zip")
        shutil.make_archive(zip_base, "zip", root_dir=out_root, base_dir=pack_dir.name)
        return pack_dir

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _ctf_info(self) -> dict:
        try:
            data = self.repo.read_challenges()
            info = data.get("ctf_info")
            return info if isinstance(info, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _entry_dirname(entry: WriteupEntry) -> str:
        cat = sanitize_folder_name(entry.category, default="misc")
        name = sanitize_folder_name(entry.name, default="challenge")
        return f"{cat}_{name}"

    @staticmethod
    def _export_entry(entry: WriteupEntry, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "README.md").write_text(entry.writeup_md, encoding="utf-8")
        if entry.solver_files:
            solver_dest = dest / "solver"
            solver_dest.mkdir(exist_ok=True)
            for src in entry.solver_files:
                target = solver_dest / src.name
                if not target.exists():
                    shutil.copy2(src, target)

    @staticmethod
    def _render_index_header(ctf_info: dict, total: int) -> List[str]:
        title = ctf_info.get("title") or "CTF Writeup Pack"
        url = ctf_info.get("url")
        user = ctf_info.get("user")
        lines = [f"# 🏁 Writeup Pack — {title}", ""]
        meta_bits = []
        if url:
            meta_bits.append(f"Platform: {url}")
        if user:
            meta_bits.append(f"User/Team: `{user}`")
        meta_bits.append(f"Tổng số bài: {total}")
        lines.append(" | ".join(meta_bits))
        lines.append("")
        return lines

    @staticmethod
    def _render_warnings(warnings: List[str]) -> List[str]:
        if not warnings:
            return []
        lines = ["## ⚠️ Cảnh báo validate", ""]
        lines.extend(f"- {w}" for w in warnings)
        lines.append("")
        return lines
