"""WriteupAssessor — heuristic chấm điểm mức độ hoàn thành writeup (100đ).

Spec: docs/superpowers/specs/2026-08-24-challenge-status-model-design.md §5.

Trục điểm:
  - Flag     35: khớp flag_format +30 · generic-flag regex +20 · xoá placeholder
                 chưa có flag mới +5
  - Evidence 30: code block KHÔNG phải boilerplate template +18 · command/output
                 thật ($ , nc , hex/base64) +7 · screenshot cục bộ +5
  - Prose    25: mục Recon có văn thật >30 từ +12 (8-30 từ +6); Exploitation
                 tương tự +13; dung lượng văn mới >500 ký tự bù tối đa đến 25
  - Checkbox 10: ``- [x]`` +10

Guard skeleton: nếu caller truyền ``reference_template`` (template sinh lại từ
``WorkspaceBuilder._generate_writeup_template()``) và similarity >= 0.95 →
SKELETON ngay, không cần tính điểm.

Heuristic CHỈ được áp khi ``status.writeup_auto == True`` (caller quyết định).
"""
from __future__ import annotations

import difflib
import re
from typing import Dict, List, Optional

from ..storage.constants import FLAG_PLACEHOLDER

SIMILARITY_SKELETON = 0.95
COMPLETE_SCORE_THRESHOLD = 70
EXPLOIT_FULL_SCORE = 13

CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.M)
GENERIC_FLAG_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{2,24}\{[^{}\n]{4,256}\}")
HEX_LIKE_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")
BASE64_LIKE_RE = re.compile(r"\b[A-Za-z0-9+/]{24,}={0,2}\b")
CHECKBOX_DONE_RE = re.compile(r"^-?\s*\*{0,2}Status\*{0,2}:.*\[x\]|^\s*- \[x\]", re.M | re.I)

# Đoạn code boilerplate của template workspace (WorkspaceBuilder) — code block
# chỉ chứa những dòng này chưa được coi là "code riêng" của người viết.
_BOILERPLATE_LINES = (
    "python3 ../solver/solve.py",
)


def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text or "", re.U))


def _section_text(md_text: str, keywords: tuple) -> str:
    """Lấy phần thân của mục heading đầu tiên khớp ``keywords`` (case-insensitive)
    cho đến heading kế tiếp. Trả "" nếu không có mục."""
    matches = list(HEADING_RE.finditer(md_text))
    for idx, m in enumerate(matches):
        low = m.group(1).lower()
        if any(k in low for k in keywords):
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(md_text)
            return md_text[m.end():end]
    return ""


def _is_boilerplate_code(code: str) -> bool:
    stripped = code.strip()
    if not stripped:
        return True
    lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
    meaningful = [ln for ln in lines if ln not in _BOILERPLATE_LINES]
    return not meaningful


def assess_writeup(md_text: str,
                   flag_format: Optional[str] = None,
                   reference_template: Optional[str] = None) -> Dict[str, object]:
    """Chấm writeup theo thang 100đ. Trả về::

        {"status": "skeleton|draft|complete", "score": int,
         "signals": {tên_tín_hiệu: bool/int}, "missing": [gợi ý tiếng Việt]}
    """
    md = md_text or ""
    signals: Dict[str, object] = {}
    missing: List[str] = []

    # ---- Guard skeleton (rẻ, chắc) -------------------------------------
    if reference_template:
        ratio = difflib.SequenceMatcher(
            None, md.strip(), reference_template.strip()).ratio()
        signals["template_similarity"] = round(ratio, 4)
        if ratio >= SIMILARITY_SKELETON:
            return {
                "status": "skeleton",
                "score": 0,
                "signals": signals,
                "missing": ["Writeup vẫn nguyên template gốc — hãy điền nội dung phân tích thật."],
            }

    # ---- Flag (max 35) ---------------------------------------------------
    flag_score = 0
    real_flag = False
    format_matched = False
    if flag_format:
        try:
            # Bỏ anchor ^/$ để search được flag nằm GIỮA văn bản nhiều dòng
            # (pattern anchored giữ nguyên cho validate_flag ở chỗ khác).
            body = flag_format.strip()
            if body.startswith("^"):
                body = body[1:]
            if body.endswith("$"):
                body = body[:-1]
            fmt_re = re.compile(body, re.M)
            for m in fmt_re.finditer(md):
                if m.group(0).strip() != FLAG_PLACEHOLDER:
                    format_matched = True   # bỏ qua chính placeholder
                    break
        except re.error:
            format_matched = False
    if format_matched:
        flag_score += 30
        real_flag = True
        signals["flag_format_matched"] = True
    else:
        generic_hits = [
            g for g in GENERIC_FLAG_RE.findall(md) if g.strip() != FLAG_PLACEHOLDER
        ]
        if generic_hits:
            flag_score += 20
            real_flag = True
            signals["generic_flag_found"] = True

    placeholder_present = FLAG_PLACEHOLDER in md
    if not real_flag and not placeholder_present and md.strip():
        # Đã xoá placeholder nhưng chưa điền flag thật.
        flag_score += 5
        signals["placeholder_removed"] = True
    signals["has_real_flag"] = real_flag
    flag_score = min(flag_score, 35)

    if not real_flag:
        missing.append(
            "Chưa có flag thật trong writeup (placeholder chưa được thay hoặc flag không khớp định dạng).")

    # ---- Evidence (max 30) -----------------------------------------------
    evidence_score = 0
    code_blocks = CODE_FENCE_RE.findall(md)
    own_code = any(not _is_boilerplate_code(c) for c in code_blocks)
    if own_code:
        evidence_score += 18
        signals["own_code_block"] = True
    else:
        missing.append("Chưa có code block riêng (code mẫu của template chưa được thay).")

    command_evidence = bool(
        re.search(r"(?m)^\s*\$\s", md)
        or re.search(r"\bnc\s+\S+\s+\d+", md)
        or HEX_LIKE_RE.search(md)
        or BASE64_LIKE_RE.search(md)
    )
    if command_evidence:
        evidence_score += 7
        signals["real_command_output"] = True

    local_shot = any(not src.lower().startswith(("http://", "https://"))
                     for src in IMAGE_RE.findall(md))
    if local_shot:
        evidence_score += 5
        signals["local_screenshot"] = True
    evidence_score = min(evidence_score, 30)

    # ---- Prose (max 25) ----------------------------------------------------
    prose_score = 0
    recon_text = _section_text(md, ("recon", "reconnaissance", "phân tích", "vulnerability"))
    recon_words = _word_count(recon_text)
    if recon_words > 30:
        prose_score += 12
        signals["recon_prose_full"] = True
    elif recon_words >= 8:
        prose_score += 6
        signals["recon_prose_partial"] = True
    else:
        missing.append("Mục 'Reconnaissance' chưa có nội dung thực.")

    exploit_text = _section_text(md, ("exploit", "poc", "khai thác"))
    exploit_words = _word_count(exploit_text)
    exploit_section_score = 0
    if exploit_words > 30:
        exploit_section_score = EXPLOIT_FULL_SCORE
        signals["exploit_prose_full"] = True
    elif exploit_words >= 8:
        exploit_section_score = 7
        signals["exploit_prose_partial"] = True
    else:
        missing.append("Mục 'Exploitation Strategy' chưa có nội dung thực.")
    prose_score += exploit_section_score

    # Bù dung lượng văn mới >500 ký tự lên tối đa 25.
    body_no_code = CODE_FENCE_RE.sub("", md)
    prose_chars = len(body_no_code.strip())
    signals["prose_chars"] = prose_chars
    if prose_score < 25 and prose_chars > 500:
        prose_score = min(25, prose_score + (25 - prose_score))
    prose_score = min(prose_score, 25)

    # ---- Checkbox (max 10) ---------------------------------------------------
    checkbox_done = bool(CHECKBOX_DONE_RE.search(md))
    checkbox_score = 10 if checkbox_done else 0
    if not checkbox_done:
        missing.append("Chưa tick marker hoàn thành ('- [x] Solved').")

    score = min(100, flag_score + evidence_score + prose_score + checkbox_score)

    complete = (
        score >= COMPLETE_SCORE_THRESHOLD
        and real_flag
        and (own_code or exploit_section_score >= EXPLOIT_FULL_SCORE)
    )
    status = "complete" if complete else "draft"

    return {
        "status": status,
        "score": score,
        "signals": signals,
        "missing": missing,
    }
