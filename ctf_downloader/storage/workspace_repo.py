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
import time
from pathlib import Path
from typing import Callable, Iterator, Union
from urllib.parse import urlparse

from .constants import (
    FLAG_PLACEHOLDER,
    FLAG_STATES,
    LIVE_RANK_PREFIX,
    SOLVED_DONE,
    SOLVED_MARKERS_DONE,
    SOLVED_TODO,
    SOLVE_RANK,
    SOLVE_STATES,
    STATUS_SCHEMA_VERSION,
    SUMMARY_FILES_LINE_PREFIX,
    WRITEUP_STATES,
)
from .fileio import (
    atomic_write_json,
    atomic_write_text,
    locked_update_json,
    locked_write_text,
)

PathLike = Union[str, Path]


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normalize_status(raw) -> dict:
    """Chuẩn hoá block ``status`` về schema v2 (bản sao sâu cho flag dict/list).

    Giá trị không hợp lệ / sai kiểu được thay bằng default — KHÔNG bao giờ raise.
    """
    src = raw if isinstance(raw, dict) else {}

    def _enum(value, allowed, default):
        return value if value in allowed else default

    flag_src = src.get("flag") if isinstance(src.get("flag"), dict) else {}
    labels = src.get("labels")
    if not isinstance(labels, list):
        labels = []

    return {
        "schema_version": STATUS_SCHEMA_VERSION,
        "solve": _enum(src.get("solve"), SOLVE_STATES, "unsolved"),
        "flag": {
            "value": flag_src.get("value"),
            "state": _enum(flag_src.get("state"), FLAG_STATES, "none"),
        },
        "writeup": _enum(src.get("writeup"), WRITEUP_STATES, "none"),
        "writeup_auto": bool(src.get("writeup_auto", True)),
        "notes": str(src.get("notes") or ""),
        "labels": [str(x) for x in labels],
        "container": _enum(src.get("container"), ("none", "running", "stopped"), "none"),
        "synced_at": src.get("synced_at"),
        "updated_at": src.get("updated_at"),
    }


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
            raw = path.read_text(encoding="utf-8-sig")
        except OSError:
            # Không đọc được: không backup được gì, trả rỗng (caller không
            # được phép ghi đè lên file này qua repo mà không đọc trước).
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
            # utf-8-sig: bỏ qua BOM UTF-8 nếu file bị editor thêm vào,
            # tránh parse fail -> mất toàn bộ name/id của challenge.
            with open(path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def write_metadata(self, path: PathLike, meta: dict) -> None:
        atomic_write_json(Path(path), meta)

    def update_metadata(self, path: PathLike, mutator: "Callable[[dict], dict | None]") -> dict:
        """Read-mutate-write TOÀN BỘ metadata.json trong CÙNG khóa flock với
        ``update_status`` (locked_update_json dùng chung lock file
        ``metadata.json.lock``).

        Mọi caller muốn sửa field ngoài block ``status`` (instance_info,
        submitted_flag, ...) PHẢI đi qua đây thay vì read_metadata +
        write_metadata — đường ghi unlocked sẽ gây lost update đa tiến trình:
        ghi đè block status/file vừa được tiến trình khác cất dưới flock.
        """
        def _mut(meta: dict) -> dict:
            result = mutator(meta if isinstance(meta, dict) else {})
            return result if isinstance(result, dict) else (meta if isinstance(meta, dict) else {})

        return locked_update_json(path, _mut)

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
    # Status đa chiều (schema v2) — spec challenge-status-model §2-§3
    # ------------------------------------------------------------------

    def _migrate_status(self, meta: dict, meta_path: "Path | None") -> dict:
        """normalize + migrate-on-read từ các field legacy (không ghi file).

        - ``solved_by_me=true`` (hoặc marker ``- [x] Solved`` trong README)
          → ``solve=solved_by_me``
        - placeholder FLAG đã bị thay bằng flag thật → ``flag=found_unverified``
        - ``instance_info.is_container`` → ``container=stopped`` (chỉ nâng,
          không đè giá trị running đã có)
        """
        st = normalize_status((meta or {}).get("status"))

        # 1. Legacy bool mirror
        if st["solve"] == "unsolved" and bool((meta or {}).get("solved_by_me")):
            st["solve"] = "solved_by_me"

        readme_texts = []
        if meta_path is not None:
            root = Path(meta_path).parent
            for rp in (root / "writeup" / "README.md", root / "README.md"):
                try:
                    readme_texts.append(rp.read_text(encoding="utf-8"))
                except OSError:
                    continue

        # 2. Marker `- [x] Solved` trong README/writeup
        if st["solve"] == "unsolved":
            for text in readme_texts:
                if any(marker in text for marker in SOLVED_MARKERS_DONE):
                    st["solve"] = "solved_by_me"
                    break

        # 3. Placeholder FLAG đã thay → flag tìm được chưa verify
        if st["flag"]["state"] == "none":
            for text in readme_texts:
                m = re.search(r"^[\s>-]*\**\s*Flag\**\s*:\s*`([^`\n]+)`", text, re.M)
                if not m:
                    continue
                val = m.group(1).strip()
                if val and val != FLAG_PLACEHOLDER:
                    st["flag"]["value"] = val
                    st["flag"]["state"] = "found_unverified"
                    break

        # 4. instance_info mirror (bare is_container -> stopped theo spec;
        #    status lạ vd 'unknown' -> KHÔNG đụng trục container)
        if st["container"] == "none":
            inst = (meta or {}).get("instance_info")
            if isinstance(inst, dict) and inst.get("is_container"):
                inst_status = inst.get("status")
                if inst_status == "running":
                    st["container"] = "running"
                elif inst_status in (None, "", "stopped"):
                    st["container"] = "stopped"

        return st

    def read_status(self, meta_path: PathLike, meta: "dict | None" = None) -> dict:
        """Đọc block ``status`` của một challenge: normalize + migrate-on-read.

        Không ghi file — workspace cũ được nâng cấp "on the fly"; lần ghi
        status đầu tiên sẽ persist schema mới.

        ``meta`` (tùy chọn): metadata.json ĐÃ đọc trước — caller quét toàn
        workspace (status scan) đã cầm metadata rồi thì truyền vào đây để
        tránh đọc JSON lại một lần nữa cho từng challenge.
        """
        meta_path = Path(meta_path)
        if meta is None:
            meta = self.read_metadata(meta_path)
        return self._migrate_status(meta, meta_path)

    def update_status(self, meta_path: PathLike, mutator: Callable[[dict], "dict | None"]) -> dict:
        """Read-mutate-write block ``status`` trong flock (lock granularity
        theo challenge — submit song song ở 2 challenge không chặn nhau).

        ``mutator(status_dict)`` nhận status đã normalize/migrate và trả về
        status mới (trả ``None`` để giữ nguyên). Sau khi mutate:
          - stamp ``updated_at``
          - mirror legacy ``solved_by_me`` (luôn == solve=='solved_by_me')
          - toggle marker README theo hướng thay đổi của trục solve
        Trả về status cuối cùng.
        """
        meta_path = Path(meta_path)
        root = meta_path.parent
        readme_paths = [root / "writeup" / "README.md", root / "README.md"]

        def _mut(meta: dict) -> dict:
            meta = dict(meta or {})
            current = self._migrate_status(meta, meta_path)
            old_solve = current["solve"]   # chốt TRƯỚC khi mutator có thể mutate in-place
            new = mutator(current)
            new = normalize_status(new if new is not None else current)
            new["updated_at"] = _now_iso()

            meta["solved_by_me"] = new["solve"] == "solved_by_me"
            meta["status"] = new

            if old_solve != new["solve"]:
                if new["solve"] == "solved_by_me":
                    self.write_solved_state(readme_paths, True)
                elif old_solve == "solved_by_me":
                    self.write_solved_state(readme_paths, False)
            return meta

        locked_update_json(meta_path, _mut)
        updated = self.read_metadata(meta_path)
        return normalize_status(updated.get("status"))

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
            # repl dạng lambda: rank_line được chèn NGUYÊN VĂN. Nếu truyền
            # rank_line làm replacement chuỗi, re.sub sẽ coi `\` là escape
            # (tên team chứa backslash làm hỏng/vỡ output).
            new_text = re.sub(r"-\s*\*\*Live Rank\*\*:[^\n]+", lambda m: rank_line, text)
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

    # ------------------------------------------------------------------
    # RANKING.md (dump scoreboard live của RankService)
    # ------------------------------------------------------------------

    @property
    def ranking_md_path(self) -> Path:
        return self.root / "RANKING.md"

    def write_ranking_md(self, content: str) -> None:
        """Ghi RANKING.md nguyên tử dưới flock (locked_write_text) — đường
        ghi DUY NHẤT cho bảng xếp hạng live. Services KHÔNG được open() thô
        (spec-audit: writer state phải đi qua storage layer)."""
        locked_write_text(self.ranking_md_path, content)
