import os
import json
from collections import defaultdict
from typing import List, Dict, Any
from ..platforms.base import Challenge, CTFInfo
from ..utils.sanitize import sanitize_folder_name

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
            by_category[chall.category].append(chall)
            total_points += chall.points
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
        lines.append(f"- **Total Files Downloaded**: {total_files}\n")

        # Category Breakdown Table
        lines.append("## 📊 Categories Overview\n")
        lines.append("| Category | Challenges | Total Points |")
        lines.append("| :--- | :--- | :--- |")
        for cat, challs in sorted(by_category.items()):
            cat_pts = sum(c.points for c in challs)
            lines.append(f"| **{cat}** | {len(challs)} | {cat_pts} |")
        lines.append("")

        # Detailed Table per Category
        for cat, challs in sorted(by_category.items()):
            lines.append(f"## 📁 {cat}\n")
            lines.append("| Challenge | Points | Solves | Files | Status | Path |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
            
            clean_cat = sanitize_folder_name(cat, default="Misc")
            for c in challs:
                clean_name = sanitize_folder_name(c.name, default=f"chall_{c.id}")
                rel_path = f"{clean_cat}/{clean_name}/README.md"
                
                c_files = all_results.get(c.id, [])
                succ_files = sum(1 for f in c_files if f.get("success"))
                files_str = f"{succ_files} file(s)" if succ_files > 0 else "-"
                
                solves_str = str(c.solves_count) if c.solves_count is not None else "-"
                status_str = "✅ Solved" if c.solved_by_me else "⏳ Unsolved"
                
                lines.append(f"| **[{c.name}]({rel_path})** | {c.points} | {solves_str} | {files_str} | {status_str} | [`{clean_cat}/{clean_name}`]({clean_cat}/{clean_name}) |")
            lines.append("")

        summary_content = "\n".join(lines)
        summary_path = os.path.join(base_output_dir, "SUMMARY.md")
        with open(summary_path, "w", encoding="utf-8") as f:
            f.write(summary_content)

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
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(json_data, f, indent=2, ensure_ascii=False)


        return summary_path
