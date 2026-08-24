"""WorkspaceRepo — nơi DUY NHẤT biết schema các state file của một workspace.

Hợp nhất logic truy cập file từng nằm rải rác ở:
  - submitter.py      (_load_challenges/_resolve_url_from_workspace/_cached_flag_format/
                       _load_submit_history/_update_local_workspace)
  - instance_manager.py (find_challenge/_update_local_instance_info/list_containers)
  - dashboard.py      (_scan_local_challenges)
  - ranking.py        (_resolve_url/_save_ranking_docs phần patch SUMMARY)

Ghi file đi qua storage.fileio (atomic + flock). Chuỗi literal chia sẻ lấy từ
storage.constants.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterator, Union
from urllib.parse import urlparse

from .constants import (
    LIVE_RANK_PREFIX,
    SOLVED_DONE,
    SOLVED_MARKERS_DONE,
    SOLVED_TODO,
    SUMMARY_FILES_LINE_PREFIX,
)
from .fileio import atomic_write_json, atomic_write_text, locked_update_json

PathLike = Union[str, Path]


class WorkspaceRepo:
    def __init__(self, root: PathLike):
        self.root = Path(root)

    # ------------------------------------------------------------------
    # challenges.json
    # ------------------------------------------------------------------

    @property
    def challenges_path(self) -> Path:
        return self.root / "challenges.json"

    def _load_json_object(self, path: Path) -> dict:
        """Đọc JSON object từ ``path``.

        File thiếu / không đọc được / JSON hỏng / hợp lệ nhưng KHÔNG phải
        dict -> nội dung cũ được backup sang ``<name>.bak`` (khi có nội dung)
        và trả về ``{}``.
        """
        if not path.exists():
            return {}
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return {}
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = None
        if not isinstance(data, dict):
            # JSON hỏng HOẶC hợp lệ nhưng sai kiểu (list/string/...) ->
            # coi như corrupt: backup rồi trả rỗng.
            try:
                path.with_name(path.name + ".bak").write_text(raw, encoding="utf-8")
            except OSError:
                pass
            return {}
        return data

    def read_challenges(self) -> dict:
        return self._load_json_object(self.challenges_path)

    def write_challenges(self, data: dict) -> None:
        locked_update_json(self.challenges_path, lambda _current: data)

    def mutate_challenges(self, mutator) -> dict:
        """Read-mutate-write challenges.json dưới lock (cho caller cần cập nhật
        từng challenge entry mà không nạp đè toàn bộ file)."""
        return locked_update_json(self.challenges_path, mutator)

    def update_ctf_info(self, **fields) -> None:
        def _mut(data: dict) -> dict:
            data = dict(data or {})
            ctf_info = dict(data.get("ctf_info") or {})
            ctf_info.update(fields)
            data["ctf_info"] = ctf_info
            return data

        locked_update_json(self.challenges_path, _mut)

    def resolve_platform_url(self) -> "str | None":
        """URL nền tảng: ctf_info.url trước, sau đó fallback qua
        submit_endpoint trong metadata.json của từng challenge."""
        data = self.read_challenges()
        url = (data.get("ctf_info") or {}).get("url")
        if url:
            return url
        for meta_path in self.iter_challenges():
            meta = self.read_metadata(meta_path)
            endpoint = meta.get("submit_endpoint")
            if endpoint:
                p = urlparse(endpoint)
                return f"{p.scheme}://{p.netloc}"
        return None

    # ------------------------------------------------------------------
    # Challenge lookup
    # ------------------------------------------------------------------

    def find_challenge(self, q) -> "dict | None":
        """Tìm challenge theo ``q``: exact id(str) -> exact name.lower() ->
        substring name. Kết quả đến từ metadata.json có kèm ``_local_path``.
        Không khớp -> None."""
        entries = []
        data = self.read_challenges()
        entries.extend(c for c in (data.get("challenges") or []) if isinstance(c, dict))

        local = []
        for meta_path in self.iter_challenges():
            meta = self.read_metadata(meta_path)
            if not meta:
                continue
            meta = dict(meta)
            meta["_local_path"] = str(meta_path.parent)
            local.append(meta)

        q_str = str(q)
        q_low = q_str.strip().lower()

        for c in entries + local:   # tier 1: exact id
            if str(c.get("id")) == q_str:
                return c
        for c in entries + local:   # tier 2: exact name
            if str(c.get("name", "")).strip().lower() == q_low:
                return c
        if q_low:
            for c in entries + local:  # tier 3: substring name
                if q_low in str(c.get("name", "")).lower():
                    return c
        return None

    # ------------------------------------------------------------------
    # metadata.json
    # ------------------------------------------------------------------

    def iter_challenges(self) -> Iterator[Path]:
        """Yield đường dẫn mọi metadata.json trong workspace (os.walk duy nhất)."""
        for dirpath, _dirnames, filenames in os.walk(self.root):
            if "metadata.json" in filenames:
                yield Path(dirpath) / "metadata.json"

    def read_metadata(self, path: PathLike) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def write_metadata(self, path: PathLike, meta: dict) -> None:
        atomic_write_json(Path(path), meta)

    @staticmethod
    def is_container(meta: dict) -> bool:
        """Predicate container rộng nhất (nguồn: instance_manager.list_containers).
        An toàn khi ``raw`` là None / thiếu field."""
        if not isinstance(meta, dict):
            return False
        inst = meta.get("instance_info")
        if not isinstance(inst, dict):
            inst = {}
        raw = meta.get("raw")
        if not isinstance(raw, dict):
            raw = {}
        tags = meta.get("tags") or []
        if inst.get("is_container"):
            return True
        if meta.get("type") == "DynamicContainer":
            return True
        if raw.get("type") in ("dynamic_docker", "DynamicContainer"):
            return True
        return any(str(t).lower() == "container" for t in tags)

    # ------------------------------------------------------------------
    # submit_history.json
    # ------------------------------------------------------------------

    @property
    def submit_history_path(self) -> Path:
        return self.root / "submit_history.json"

    def load_submit_history(self) -> dict:
        """Schema ``{"entries": [...]}``; corrupt (hỏng hoặc không phải dict)
        -> backup .bak + trả ``{"entries": []}``."""
        data = self._load_json_object(self.submit_history_path)
        if not data:
            return {"entries": []}
        raw_entries = data.get("entries", [])
        return {"entries": [e for e in raw_entries if isinstance(e, dict)]}

    def save_submit_history(self, hist: dict) -> None:
        entries = hist.get("entries", []) if isinstance(hist, dict) else []
        locked_update_json(
            self.submit_history_path,
            lambda _current: {"entries": list(entries)},
        )

    # ------------------------------------------------------------------
    # Solved-state markers (writeup/README.md, README.md)
    # ------------------------------------------------------------------

    def read_solved_state(self, readme_paths) -> bool:
        """True nếu bất kỳ file nào chứa marker đã-solve (constants.SOLVED_MARKERS_DONE)."""
        for rp in readme_paths:
            rp = Path(rp)
            if not rp.exists():
                continue
            try:
                text = rp.read_text(encoding="utf-8")
            except OSError:
                continue
            if any(marker in text for marker in SOLVED_MARKERS_DONE):
                return True
        return False

    def write_solved_state(self, readme_paths, solved: bool) -> int:
        """Đổi marker ``- [ ] Solved`` <-> ``- [x] Solved`` trong các file chỉ định.
        Trả về số file thực sự bị thay đổi."""
        src, dst = (SOLVED_TODO, SOLVED_DONE) if solved else (SOLVED_DONE, SOLVED_TODO)
        changed = 0
        for rp in readme_paths:
            rp = Path(rp)
            if not rp.exists():
                continue
            try:
                text = rp.read_text(encoding="utf-8")
                if src not in text:
                    continue
                atomic_write_text(rp, text.replace(src, dst))
                changed += 1
            except OSError:
                continue
        return changed

    # ------------------------------------------------------------------
    # SUMMARY.md live-rank patch
    # ------------------------------------------------------------------

    def patch_summary_live_rank(self, rank_line: str) -> bool:
        """Chèn ``rank_line`` vào SUMMARY.md ngay trước dòng
        ``- **Total Files Downloaded**:``, hoặc thay dòng Live Rank cũ nếu đã có.
        Trả False nếu SUMMARY.md thiếu hoặc không có điểm neo."""
        summary_path = self.root / "SUMMARY.md"
        if not summary_path.exists():
            return False
        try:
            text = summary_path.read_text(encoding="utf-8")
        except OSError:
            return False

        if LIVE_RANK_PREFIX in text:
            new_text = re.sub(r"-\s*\*\*Live Rank\*\*:[^\n]+", rank_line, text)
        elif SUMMARY_FILES_LINE_PREFIX in text:
            new_text = text.replace(
                SUMMARY_FILES_LINE_PREFIX, f"{rank_line}\n{SUMMARY_FILES_LINE_PREFIX}", 1
            )
        else:
            return False

        try:
            atomic_write_text(summary_path, new_text)
        except OSError:
            return False
        return True
