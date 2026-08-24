import os
import json
from typing import List, Dict, Any, Optional
from ..platforms.base import Challenge
from ..extractors.link_extractor import ExtractedLink, ConnectionInfo
from ..extractors.text_parser import TextParser
from ..storage.constants import FLAG_PLACEHOLDER, TARGET_CONNECTION_FMT
from ..utils.sanitize import sanitize_folder_name
from ..utils.logger import Logger

class WorkspaceBuilder:
    @staticmethod
    def create_challenge_workspace(
        base_output_dir: str,
        challenge: Challenge,
        extracted_links: List[ExtractedLink],
        connections: List[ConnectionInfo],
        download_results: List[Dict[str, Any]],
        create_solve_template: bool = True
    ) -> str:
        """
        Creates directory structure for a challenge and generates README.md, metadata.json, and solve.py.
        Returns the created challenge folder path.
        """
        clean_category = sanitize_folder_name(challenge.category, default="Misc")
        clean_name = sanitize_folder_name(challenge.name, default=f"chall_{challenge.id}")
        
        challenge_dir = os.path.join(base_output_dir, clean_category, clean_name)
        os.makedirs(challenge_dir, exist_ok=True)

        # Create structured subdirectories for professional modularity
        challenge_sub_dir = os.path.join(challenge_dir, "challenge")
        script_sub_dir = os.path.join(challenge_dir, "script")
        solver_sub_dir = os.path.join(challenge_dir, "solver")
        writeup_sub_dir = os.path.join(challenge_dir, "writeup")
        os.makedirs(challenge_sub_dir, exist_ok=True)
        os.makedirs(script_sub_dir, exist_ok=True)
        os.makedirs(solver_sub_dir, exist_ok=True)
        os.makedirs(writeup_sub_dir, exist_ok=True)

        # Copy downloaded files to challenge/ subdirectory
        import shutil
        for dl in download_results:
            if dl.get("success") and dl.get("saved_path") and os.path.isfile(dl["saved_path"]):
                target_copy = os.path.join(challenge_sub_dir, os.path.basename(dl["saved_path"]))
                if not os.path.exists(target_copy):
                    try:
                        shutil.copy2(dl["saved_path"], target_copy)
                    except Exception:
                        pass

        # 1. Generate challenge/README.md (Original Challenge Description & Resources)
        challenge_readme_path = os.path.join(challenge_sub_dir, "README.md")
        if not os.path.exists(challenge_readme_path):
            readme_content = WorkspaceBuilder._generate_readme(
                challenge, extracted_links, connections, download_results
            )
            with open(challenge_readme_path, "w", encoding="utf-8") as f:
                f.write(readme_content)

        # 2. Generate challenge/NOTE.md (Workspace Guidelines)
        challenge_note_path = os.path.join(challenge_sub_dir, "NOTE.md")
        if not os.path.exists(challenge_note_path):
            note_content = """# 📌 Quy Tắc Tổ Chức Thư Mục (Workspace Guidelines)

- **`script/`**: Thư mục workspace nháp. Hãy viết toàn bộ script test, payload thử nghiệm, fuzzing, giải mã linh tinh tại đây để tránh làm rác thư mục gốc.
- **`solver/`**: Khi script giải bài hoàn thiện và lấy được flag thành công, hãy chuyển/lưu script chính thức vào thư mục `solver/` (ví dụ `solver/solve.py`).
- **`writeup/`**: Thư mục viết báo cáo, phân tích kỹ thuật và ghi lại Flag sau khi giải xong bài.
"""
            with open(challenge_note_path, "w", encoding="utf-8") as f:
                f.write(note_content)

        # 3. Generate writeup/README.md (Blank Writeup Template for after solving)
        writeup_path = os.path.join(writeup_sub_dir, "README.md")
        if not os.path.exists(writeup_path):
            writeup_content = WorkspaceBuilder._generate_writeup_template(challenge)
            with open(writeup_path, "w", encoding="utf-8") as f:
                f.write(writeup_content)

        # 2. Generate metadata.json
        meta_data = {
            "id": challenge.id,
            "name": challenge.name,
            "category": challenge.category,
            "points": challenge.points,
            "author": challenge.author,
            "tags": challenge.tags,
            "hints": challenge.hints,
            "connection_info": challenge.connection_info,
            "solved_by_me": challenge.solved_by_me,
            "solves_count": challenge.solves_count,
            "submit_endpoint": challenge.submit_endpoint,
            "instance_info": challenge.instance_info,
            "downloaded_files": download_results,
            "raw": challenge.raw_data
        }
        meta_path = os.path.join(challenge_dir, "metadata.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(meta_data, f, indent=2, ensure_ascii=False)

        # 3. Generate solver/solve.py if requested and doesn't exist
        solver_solve_path = os.path.join(solver_sub_dir, "solve.py")
        if create_solve_template and not os.path.exists(solver_solve_path):
            solve_script = WorkspaceBuilder._generate_solve_template(challenge, connections)
            with open(solver_solve_path, "w", encoding="utf-8") as f:
                f.write(solve_script)

        return challenge_dir

    @staticmethod
    def _generate_readme(
        challenge: Challenge,
        extracted_links: List[ExtractedLink],
        connections: List[ConnectionInfo],
        download_results: List[Dict[str, Any]]
    ) -> str:
        """
        Builds a comprehensive markdown documentation for the challenge.
        """
        lines = []
        lines.append(f"# {challenge.name}\n")
        
        # Metadata table
        lines.append("| Property | Value |")
        lines.append("| :--- | :--- |")
        lines.append(f"| **Category** | `{challenge.category}` |")
        lines.append(f"| **Points** | `{challenge.points}` |")
        if challenge.author:
            lines.append(f"| **Author** | {challenge.author} |")
        if challenge.solves_count is not None:
            lines.append(f"| **Solves** | {challenge.solves_count} |")
        if challenge.tags:
            tags_str = ", ".join([f"`{t}`" for t in challenge.tags])
            lines.append(f"| **Tags** | {tags_str} |")
        lines.append("")

        # Connection info / Netcat / Web service
        if challenge.connection_info or connections:
            lines.append("## 🔌 Connection / Service")
            if challenge.connection_info:
                lines.append(f"```bash\n{challenge.connection_info}\n```\n")
            for conn in connections:
                if conn.proto == "nc":
                    lines.append(f"```bash\nnc {conn.host} {conn.port}\n```\n")
                elif conn.proto in ["http", "https"]:
                    lines.append(f"- URL: [{conn.raw_command}]({conn.raw_command})\n")
                elif conn.proto == "ssh":
                    lines.append(f"```bash\n{conn.raw_command}\n```\n")

        # Description
        lines.append("## 📝 Description\n")
        parsed_desc = TextParser.html_to_markdown(challenge.description)
        if parsed_desc:
            lines.append(parsed_desc)
        else:
            lines.append("*No description provided.*")
        lines.append("\n")

        # Hints
        if challenge.hints:
            lines.append("## 💡 Hints\n")
            for idx, hint in enumerate(challenge.hints, 1):
                h_content = hint.get("content") or hint.get("hint") or str(hint)
                cost = hint.get("cost", 0)
                if cost > 0:
                    lines.append(f"- **Hint {idx}** (Cost: {cost} pts): {h_content}")
                else:
                    lines.append(f"- **Hint {idx}**: {h_content}")
            lines.append("")

        # Downloaded Files / Attachments
        lines.append("## 📦 Files & Resources\n")
        if download_results:
            lines.append("| File / Resource | Source | Status | Local Path / URL |")
            lines.append("| :--- | :--- | :--- | :--- |")
            for res in download_results:
                name = res.get("name") or "attachment"
                src = res.get("source", "direct")
                status = "✅ Downloaded" if res.get("success") else "❌ Failed"
                local_rel = os.path.basename(res.get("saved_path")) if res.get("saved_path") else res.get("url")
                lines.append(f"| `{name}` | `{src}` | {status} | [{local_rel}]({local_rel}) |")
            lines.append("")
        else:
            lines.append("*No file attachments associated with this challenge.*\n")

        # 3rd Party Links
        third_party_links = [l for l in extracted_links if l.link_type != "direct_file"]
        if third_party_links:
            lines.append("### 🔗 External Links in Description\n")
            for l in third_party_links:
                lines.append(f"- [{l.title or l.url}]({l.url}) (`{l.link_type}`)")
            lines.append("")

        # Flag & Solution tracking
        lines.append("## 🚩 Flag & Solution\n")
        status_box = "[x]" if challenge.solved_by_me else "[ ]"
        lines.append(f"- {status_box} Solved\n")
        lines.append(f"```\n{FLAG_PLACEHOLDER}\n```\n")
        lines.append("### Writeup / Notes\n")
        lines.append("*(Write your solution steps and notes here)*\n")

        return "\n".join(lines)

    @staticmethod
    def _generate_solve_template(challenge: Challenge, connections: List[ConnectionInfo]) -> str:
        """
        Generates an automated starter solve.py template matching category.
        """
        cat_lower = challenge.category.lower()

        # Check if there is a netcat connection
        nc_conn = next((c for c in connections if c.proto == "nc"), None)
        http_conn = next((c for c in connections if c.proto in ["http", "https"]), None)

        if "pwn" in cat_lower or "rev" in cat_lower or nc_conn:
            host_str = f"'{nc_conn.host}'" if nc_conn else "'localhost'"
            port_str = str(nc_conn.port) if nc_conn and nc_conn.port else "1337"
            
            return f'''#!/usr/bin/env python3
# Solution for: {challenge.name} ({challenge.category})
from pwn import *

HOST = {host_str}
PORT = {port_str}

context.log_level = 'debug'
# context.arch = 'amd64'
# context.terminal = ['tmux', 'splitw', '-h']

def solve():
    if args.REMOTE:
        r = remote(HOST, PORT)
    else:
        # r = process('./vuln')
        r = remote(HOST, PORT)

    # TODO: Exploit logic here
    # r.sendlineafter(b'> ', b'payload')

    r.interactive()

if __name__ == '__main__':
    solve()
'''

        elif "web" in cat_lower or http_conn:
            target_url = http_conn.raw_command if http_conn else "http://target.ctf"
            return f'''#!/usr/bin/env python3
# Solution for: {challenge.name} ({challenge.category})
import requests
import re

TARGET_URL = "{target_url}"
session = requests.Session()

def solve():
    print(f"[*] Attacking: {{TARGET_URL}}")
    resp = session.get(TARGET_URL)
    print(f"[*] Status: {{resp.status_code}}")

    # TODO: Exploit logic here

if __name__ == '__main__':
    solve()
'''

        elif "crypto" in cat_lower:
            return f'''#!/usr/bin/env python3
# Solution for: {challenge.name} ({challenge.category})
from Crypto.Util.number import *
import hashlib

def solve():
    # TODO: Crypto math / decryption logic here
    pass

if __name__ == '__main__':
    solve()
'''

        else:
            return f'''#!/usr/bin/env python3
# Solution for: {challenge.name} ({challenge.category})

def solve():
    # TODO: Solution script
    print("[*] Solving {challenge.name}...")

if __name__ == '__main__':
    solve()
'''

    @staticmethod
    def _generate_writeup_template(challenge: Challenge) -> str:
        """
        Generates a standardized writeup markdown template for the challenge.
        """
        return f"""# Writeup: {challenge.name}

| Property | Value |
| :--- | :--- |
| **Category** | `{challenge.category}` |
| **Points** | `{challenge.points}` |
| **Author** | `{challenge.author or '-'}` |
| **Solves** | `{challenge.solves_count or 0}` |

---

## 📝 Challenge Overview

{challenge.description or 'No description provided.'}

---

## 🔍 Reconnaissance & Vulnerability Analysis

{TARGET_CONNECTION_FMT.format(info=challenge.connection_info or '-')}
- Category: `{challenge.category}`
- Key observations & vulnerability hypothesis:
  *(Document reverse engineering, source code review, or protocol analysis here)*

---

## 💻 Exploitation Strategy & PoC

Exploit script is located at [`../solver/solve.py`](../solver/solve.py).

```bash
python3 ../solver/solve.py
```

---

## 🚩 Flag

- Status: `- [ ] Solved`
- Flag: `{FLAG_PLACEHOLDER}`
"""

