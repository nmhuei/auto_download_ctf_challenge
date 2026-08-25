import math
import os
from collections import defaultdict
from typing import List, Dict, Any
from ..platforms.base import Challenge, CTFInfo
from ..storage.constants import DEFAULT_CATEGORY, SOLVED_EMOJI_DONE, SUMMARY_FILES_LINE
from ..storage.fileio import atomic_write_text
from ..storage.workspace_repo import WorkspaceRepo
from ..utils.sanitize import sanitize_folder_name


def _safe_int(value) -> int:
    """Ép points về int an toàn: None / chuỗi không số / kiểu lạ -> 0.

    Platform thật (gzCTF/rCTF dynamic scoring) trả ``points: null`` rất phổ
    biến — không ép sẽ crash cả pipeline download ở bước cuối.

    ``OverflowError``: ``int(float('inf'))`` — Python json.loads chấp nhận
    literal ``Infinity`` từ platform API nên đường vào là thật.
    ``ValueError`` đã phủ ``int(float('nan'))`` và chuỗi như ``"1e400"``.
    """
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


def _json_safe(obj):
    """Đệ quy thay float NaN/Inf bằng None: json.dump mặc định allow_nan=True
    tạo literal ``NaN``/``Infinity`` mà parser strict JSON (jq...) không đọc được."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _safe_category(category) -> str:
    """Category None/rỗng -> nhóm default, tránh trộn None với str khi sorted()."""
    if category is None or not str(category).strip():
        return DEFAULT_CATEGORY
    return str(category)

class SummaryGenerator:
    @staticmethod
    def generate_summary(
        base_output_dir: str,
        ctf_info: CTFInfo,
        all_results: Dict[Any, List[Dict[str, Any]]]  # challenge_id -> list of download result dicts
    ) -> str:
        """
        Generates SUMMARY.md and challenges.json in base_output_dir.
        """
        os.makedirs(base_output_dir, exist_ok=True)
        challenges = ctf_info.challenges

        # Group by category
        by_category = defaultdict(list)
        total_points = 0
        total_files = 0
        
        for chall in challenges:
            by_category[_safe_category(chall.category)].append(chall)
            total_points += _safe_int(chall.points)
            chall_files = all_results.get(chall.id, [])
            total_files += sum(1 for f in chall_files if f.get("success"))

        # Build SUMMARY.md
        lines = []
        title = ctf_info.title or "CTF Challenges Summary"
        lines.append(f"# 🏆 {title}\n")
        
        if ctf_info.url:
            lines.append(f"- **URL**: {ctf_info.url}")
        if ctf_info.user_name:
            lines.append(f"- **User**: `{ctf_info.user_name}`")
        if ctf_info.platform_type:
            lines.append(f"- **Platform Engine**: `{ctf_info.platform_type.upper()}`")
            
        lines.append(f"- **Total Challenges**: {len(challenges)}")
        lines.append(f"- **Total Categories**: {len(by_category)}")
        lines.append(f"- **Total Points Available**: {total_points}")
        lines.append(SUMMARY_FILES_LINE.format(total_files=total_files))

        # Category Breakdown Table
        lines.append("## 📊 Categories Overview\n")
        lines.append("| Category | Challenges | Total Points |")
        lines.append("| :--- | :--- | :--- |")
        for cat, challs in sorted(by_category.items(), key=lambda kv: str(kv[0])):
            cat_pts = sum(_safe_int(c.points) for c in challs)
            lines.append(f"| **{cat}** | {len(challs)} | {cat_pts} |")
        lines.append("")

        # Detailed Table per Category
        for cat, challs in sorted(by_category.items(), key=lambda kv: str(kv[0])):
            lines.append(f"## 📁 {cat}\n")
            lines.append("| Challenge | Points | Solves | Files | Status | Path |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
            
            clean_cat = sanitize_folder_name(cat, default="Misc")
            for c in challs:
                clean_name = sanitize_folder_name(c.name, default=f"chall_{c.id}")
                rel_path = f"{clean_cat}/{clean_name}/writeup/README.md"
                
                c_files = all_results.get(c.id, [])
                succ_files = sum(1 for f in c_files if f.get("success"))
                files_str = f"{succ_files} file(s)" if succ_files > 0 else "-"
                
                solves_str = str(c.solves_count) if c.solves_count is not None else "-"
                status_str = SOLVED_EMOJI_DONE if c.solved_by_me else "⏳ Unsolved"
                
                lines.append(f"| **[{c.name}]({rel_path})** | {c.points} | {solves_str} | {files_str} | {status_str} | [`{clean_cat}/{clean_name}`]({clean_cat}/{clean_name}) |")
            lines.append("")

        summary_content = "\n".join(lines)
        summary_path = os.path.join(base_output_dir, "SUMMARY.md")

        # XCHECK hunter-c9: hai file tổng hợp này từng được ghi TRỰC TIẾP
        # (open 'w' không atomic, không flock) trong khi rank-patcher/
        # dashboard ghi cùng lúc qua WorkspaceRepo (atomic+flock) -> nội dung
        # rách/ghi đè lost-update. Chuyển hết sang storage helpers, GIỮ
        # nguyên format output (json indent=2 ensure_ascii=False, SUMMARY text).
        atomic_write_text(summary_path, summary_content)

        # Build challenges.json
        json_data = {
            "ctf_info": {
                "title": ctf_info.title,
                "url": ctf_info.url,
                "platform": ctf_info.platform_type,
                "user": ctf_info.user_name,
                "team": ctf_info.team_name,
                "game_id": ctf_info.game_id
            },
            "total_challenges": len(challenges),
            "total_points": total_points,
            "categories": {cat: len(challs) for cat, challs in by_category.items()},
            "challenges": [
                {
                    "id": c.id,
                    "name": c.name,
                    "category": c.category,
                    "points": c.points,
                    "author": c.author,
                    "tags": c.tags,
                    "hints": c.hints,
                    "connection_info": c.connection_info,
                    "solved_by_me": c.solved_by_me,
                    "solves_count": c.solves_count,
                    "submit_endpoint": c.submit_endpoint,
                    "instance_info": c.instance_info,
                    "files": all_results.get(c.id, [])
                }
                for c in challenges
            ]
        }
        json_path = os.path.join(base_output_dir, "challenges.json")
        # locked_update_json: ghi đè toàn bộ dưới flock riêng challenges.json.lock
        # (mutator bỏ qua state hiện tại — semantics overwrite như cũ), atomic
        # tmp+replace trong phạm vi khóa. _json_safe áp TRƯỚC để giữ hành vi
        # thay NaN/Inf -> None như bản ghi trực tiếp trước đây.
        WorkspaceRepo(base_output_dir).mutate_challenges(
            lambda _current: _json_safe(json_data)
        )

        return summary_path
