import os
import json
from typing import List, Dict, Any, Optional, Callable
from ..platforms.base import Challenge
from ..extractors.link_extractor import ExtractedLink, ConnectionInfo
from ..extractors.text_parser import TextParser
from ..storage.constants import FLAG_PLACEHOLDER, TARGET_CONNECTION_FMT, DEFAULT_CATEGORY
from ..storage.fileio import locked_write_text
from ..utils.sanitize import sanitize_folder_name
from ..utils.logger import Logger

class WorkspaceBuilder:
    @staticmethod
    def _safe_category(challenge: Any) -> str:
        """
        Category an toàn để so khớp/lower(): None, rỗng, toàn khoảng trắng hoặc
        không phải chuỗi -> DEFAULT_CATEGORY. Challenge dị dạng từ platform
        không được làm crash pipeline tạo workspace.
        """
        cat = getattr(challenge, "category", None)
        if isinstance(cat, str) and cat.strip():
            return cat.strip()
        return DEFAULT_CATEGORY

    @staticmethod
    def _norm_hints(hints: Any) -> List[Dict[str, Any]]:
        """
        Chuẩn hoá hints (có thể dị dạng: list[str], cost null, phần tử lạ)
        về list[{"content": str, "cost": int}] để render README. Không bao giờ raise.
        """
        norm: List[Dict[str, Any]] = []
        if not hints or not isinstance(hints, (list, tuple)):
            return norm
        for hint in hints:
            if isinstance(hint, dict):
                content = hint.get("content") or hint.get("hint") or ""
                cost_raw = hint.get("cost", 0)
                try:
                    cost = int(cost_raw) if cost_raw is not None else 0
                except (TypeError, ValueError):
                    cost = 0
                norm.append({"content": str(content), "cost": max(cost, 0)})
            elif isinstance(hint, str):
                norm.append({"content": hint, "cost": 0})
            else:
                norm.append({"content": str(hint), "cost": 0})
        return norm
    @staticmethod
    def _existing_owner_id(challenge_dir: str) -> Optional[Any]:
        """Id của challenge đang sở hữu ``challenge_dir`` (đọc metadata.json).

        None nếu không xác định được (thư mục trống / metadata thiếu-hỏng) —
        khi đó KHÔNG coi là có chủ, giữ nguyên hành vi tái sử dụng thư mục.
        """
        try:
            with open(os.path.join(challenge_dir, "metadata.json"),
                      encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data.get("id")
        except Exception:
            pass
        return None

    @staticmethod
    def resolve_challenge_dir(base_output_dir: str, challenge: Any) -> str:
        """HÀM DUY NHẤT quyết định thư mục cuối của một challenge trong
        workspace: ``sanitize(category)/sanitize(name)`` + guard tách
        ``<name>-<id>`` khi thư mục đã thuộc về challenge khác id (C9-01).

        C19-H1: hàm này phải dùng chung bởi ``PullService._full_process``
        (tính ĐÍCH TẢI một lần TRƯỚC khi download) và
        ``create_challenge_workspace``. Hai đường tự tính riêng thì nơi tải
        attachment là thư mục CHƯA áp guard — hai challenge sanitize trùng
        tên ('web:1' vs 'web/1') cùng ghi vào một ``challenge/`` và thread
        sau đè mất attachment của chủ sở hữu im lặng (per-target lock của
        downloader chỉ chống race .part trên CÙNG một URL, không chống việc
        hai challenge quyết định chung một thư mục).

        Thư mục chưa có metadata.json đọc được coi như KHÔNG có chủ (giữ
        hành vi tái sử dụng); cùng id (pull lại/--update) tái sử dụng như cũ.
        """
        clean_category = sanitize_folder_name(
            WorkspaceBuilder._safe_category(challenge), default=DEFAULT_CATEGORY
        )
        clean_name = sanitize_folder_name(challenge.name,
                                          default=f"chall_{challenge.id}")
        challenge_dir = os.path.join(base_output_dir, clean_category, clean_name)

        if os.path.isdir(challenge_dir):
            owner_id = WorkspaceBuilder._existing_owner_id(challenge_dir)
            if owner_id is not None and str(owner_id) != str(challenge.id):
                challenge_dir = os.path.join(
                    base_output_dir, clean_category, f"{clean_name}-{challenge.id}"
                )
        return challenge_dir

    @staticmethod
    def create_challenge_workspace(
        base_output_dir: str,
        challenge: Challenge,
        extracted_links: List[ExtractedLink],
        connections: List[ConnectionInfo],
        download_results: List[Dict[str, Any]],
        create_solve_template: bool = True,
        challenge_dir: Optional[str] = None
    ) -> str:
        """
        Creates directory structure for a challenge and generates README.md, metadata.json, and solve.py.
        Returns the created challenge folder path.

        ``challenge_dir``: thư mục đã được tính TRƯỚC bằng
        :meth:`resolve_challenge_dir` (C19-H1 — đích tải một lần trước khi
        download). Bỏ trống thì tự resolve như cũ (tương thích caller cũ).
        """
        if challenge_dir is None:
            challenge_dir = WorkspaceBuilder.resolve_challenge_dir(
                base_output_dir, challenge
            )

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

        def _safe_generate(path: str, producer, *, refresh: bool = False) -> None:
            """
            Sinh 1 file section (README/writeup/solve.py): một challenge dị dạng
            làm producer raise thì ghi nội dung lỗi thay vì sập cả workspace.

            C19-L7: ghi ATOMIC qua storage.fileio.locked_write_text (cùng khóa
            fcntl + atomic replace với metadata.json) thay vì open('w') trần —
            reader (watch/index) không bao giờ thấy file nửa chừng, không để
            lại .tmp/.lock.
            - refresh=True (nội dung THUẦN DERIVED từ platform —
              challenge/README.md): LUÔN viết lại. Exists-guard cũ khiến file
              này không bao giờ được viết lại sau lần đầu — --update đổi
              description/points/files thì README stale vĩnh viễn trong khi
              metadata.json đã mới.
            - refresh=False (file USER-OWNED — writeup/README.md,
              solver/solve.py): chỉ sinh LẦN ĐẦU; exists-guard giữ nguyên để
              không xoá nội dung user đã viết (guard idempotent của đường
              incremental).
            Producer raise trên file ĐÃ CÓ nội dung tốt -> giữ nguyên bản cũ,
            KHÔNG đè bằng trang lỗi.
            """
            # Thư mục cha biến mất giữa lúc dựng (bị xoá giữa chừng / skip
            # chống zombie BUG-C16-1): bỏ qua section file một cách sạch sẽ —
            # việc skip được metadata.json báo cáo rõ ràng ở cuối build.
            if not os.path.isdir(os.path.dirname(path)):
                return
            exists = os.path.exists(path)
            if exists and not refresh:
                return
            try:
                content = producer()
            except Exception as e:
                if exists:
                    Logger.warning(
                        f"Không tái sinh {os.path.basename(path)}: "
                        f"{type(e).__name__}: {str(e)[:200]} — giữ nguyên bản cũ."
                    )
                    return
                Logger.warning(
                    f"Không sinh được {os.path.basename(path)}: "
                    f"{type(e).__name__}: {str(e)[:200]}"
                )
                content = (
                    f"# Lỗi sinh nội dung tự động\n\n"
                    f"({type(e).__name__}: {str(e)[:200]})\n\n"
                    f"Dữ liệu challenge từ platform có thể dị dạng — kiểm tra `metadata.json`.\n"
                )
            if not locked_write_text(path, content):
                Logger.warning(
                    f"Không ghi được {os.path.basename(path)} tại {path} — "
                    f"thư mục đã bị xoá giữa lúc dựng workspace."
                )

        # 1. Generate challenge/README.md (Original Challenge Description & Resources)
        # C19-L7: nội dung DERIVED — refresh=True để luôn phản ánh dữ liệu
        # platform mới nhất sau --update (bỏ exists-guard stale vĩnh viễn).
        challenge_readme_path = os.path.join(challenge_sub_dir, "README.md")
        _safe_generate(challenge_readme_path, lambda: WorkspaceBuilder._generate_readme(
            challenge, extracted_links, connections, download_results
        ), refresh=True)

        # 2. Generate challenge/NOTE.md (Workspace Guidelines)
        # C19-L7: boilerplate tĩnh — ghi atomic, không cần exists-guard.
        challenge_note_path = os.path.join(challenge_sub_dir, "NOTE.md")
        note_content = """# 📌 Quy Tắc Tổ Chức Thư Mục (Workspace Guidelines)

- **`script/`**: Thư mục workspace nháp. Hãy viết toàn bộ script test, payload thử nghiệm, fuzzing, giải mã linh tinh tại đây để tránh làm rác thư mục gốc.
- **`solver/`**: Khi script giải bài hoàn thiện và lấy được flag thành công, hãy chuyển/lưu script chính thức vào thư mục `solver/` (ví dụ `solver/solve.py`).
- **`writeup/`**: Thư mục viết báo cáo, phân tích kỹ thuật và ghi lại Flag sau khi giải xong bài.
"""
        if os.path.isdir(challenge_sub_dir):
            if not locked_write_text(challenge_note_path, note_content):
                Logger.warning(
                    f"Không ghi được NOTE.md tại {challenge_note_path} — "
                    f"thư mục đã bị xoá giữa lúc dựng workspace."
                )

        # 3. Generate writeup/README.md (Blank Writeup Template for after solving)
        # USER-OWNED: chỉ sinh lần đầu (exists-guard), lần ghi đầu atomic.
        writeup_path = os.path.join(writeup_sub_dir, "README.md")
        _safe_generate(writeup_path, lambda: WorkspaceBuilder._generate_writeup_template(challenge))

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
        # Guard: raw_data dị dạng (vd chứa set()) không được làm crash việc
        # tạo workspace -> serialize ra chuỗi trước; nếu không serializable
        # thì fallback ép kiểu string (default=str) kèm warning.
        try:
            payload = json.dumps(meta_data, indent=2, ensure_ascii=False)
        except (TypeError, ValueError) as e:
            Logger.warning(
                f"metadata.json chứa dữ liệu không serializable "
                f"(challenge '{challenge.name}'): {type(e).__name__}: {str(e)[:120]} "
                f"-> fallback default=str."
            )
            payload = json.dumps(meta_data, indent=2, ensure_ascii=False, default=str)
        # XCHECK hunter-c15: metadata.json là state file — ghi THẲNG open('w')
        # ở đây race lost-update với sync/update_status vốn ghi dưới flock
        # ``metadata.json.lock`` (redownload giữa lúc sync đang chạy). Chuyển
        # qua locked_write_text: cùng khóa + atomic replace, GIỮ NGUYÊN format
        # payload (json.dumps indent=2, ensure_ascii=False, không newline cuối).
        # Review 3e0fbcc-F1: locked_write_text trả False khi thư mục challenge
        # bị xoá giữa lúc build (skip chống zombie BUG-C16-1) — KHÔNG được
        # nuốt im lặng: warning rõ tên + đường dẫn để user biết workspace này
        # thiếu metadata.json (builder giữ contract trả challenge_dir, không
        # crash pipeline như các nhánh dị dạng khác).
        if not locked_write_text(meta_path, payload):
            Logger.warning(
                f"Không ghi được metadata.json cho challenge "
                f"'{challenge.name}' tại {meta_path} — thư mục đã bị xoá "
                f"giữa lúc dựng workspace; bỏ qua lần ghi (workspace này "
                f"THIẾU metadata.json, chạy lại pull để dựng lại)."
            )

        # 3. Generate solver/solve.py if requested and doesn't exist
        solver_solve_path = os.path.join(solver_sub_dir, "solve.py")
        if create_solve_template:
            _safe_generate(solver_solve_path, lambda: WorkspaceBuilder._generate_solve_template(challenge, connections))

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

        # Hints — qua _norm_hints để chịu được hints dị dạng (list[str],
        # cost null, ...) mà không crash toàn bộ workspace
        if challenge.hints:
            lines.append("## 💡 Hints\n")
            for idx, h in enumerate(WorkspaceBuilder._norm_hints(challenge.hints), 1):
                if h["cost"] > 0:
                    lines.append(f"- **Hint {idx}** (Cost: {h['cost']} pts): {h['content']}")
                else:
                    lines.append(f"- **Hint {idx}**: {h['content']}")
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

    _CUSTOM_SOLVER_TEMPLATES: dict = {}

    @classmethod
    def register_solver_template(cls, category_keyword: str, template_fn: Callable[[Challenge, list], str]) -> None:
        """Đăng ký template solve script tùy biến theo từ khóa category."""
        cls._CUSTOM_SOLVER_TEMPLATES[category_keyword.lower()] = template_fn

    @classmethod
    def _generate_solve_template(cls, challenge: Challenge, connections: list[ConnectionInfo]) -> str:
        """
        Generates a python boilerplate exploit script tailored to category/connections.
        """
        cat_lower = WorkspaceBuilder._safe_category(challenge).lower()

        for kw, fn in cls._CUSTOM_SOLVER_TEMPLATES.items():
            if kw in cat_lower:
                return fn(challenge, connections)

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

