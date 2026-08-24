"""
SP1 — Tiện ích nhận diện & kiểm tra định dạng flag.
Các hàm thuần (pure functions), không I/O, dễ test.
"""
import re
from collections import Counter
from typing import List, Optional

# Candidate fallback: PREFIX{body}
_FLAG_CANDIDATE_RE = re.compile(r'\b([A-Za-z][A-Za-z0-9_]{1,24})\{([^{}\n]{1,256})\}')

# Từ khoá gợi ý dòng đang mô tả định dạng flag
_FORMAT_HINT_WORDS = ("flag", "format", "định dạng")

# Body placeholder rõ ràng -> bỏ qua
_PLACEHOLDER_BODIES = {
    "...", "..", "…", "xxx", "xxxx", "<flag>", "<...>", "your_flag",
    "your_flag_here", "flag_here", "flag_content", "example_flag",
}

# (a) Regex tường minh kiểu /^PREFIX{.*}$/ hoặc /^PREFIX\{.*\}$/ (brace có thể bị escape)
_EXPLICIT_FMT_RE = re.compile(
    r'/\^\s*([A-Za-z][A-Za-z0-9_]{0,24})\s*(?:\\)?\{[^/]{1,100}?(?:\\)?\}\s*(?:\\)?\$\s*/'
)

# Code span `PREFIX{body}` hoặc `^PREFIX\{body\}$`
_CODE_SPAN_RE = re.compile(r'`([^`\n]{1,160})`')
_SPAN_FMT_RE = re.compile(
    r'^\s*\^?\s*([A-Za-z][A-Za-z0-9_]{0,24})(?:\\)?\{(.+?)(?:\\)?\}\s*\$?\s*$'
)


def build_format_regex(prefix: str) -> str:
    """
    Chuyển prefix thành regex neo đầy đủ: ^<prefix_escaped>\\{.+\\}$
    """
    return "^" + re.escape(prefix) + r"\{.+\}$"


def _is_placeholder_body(body: str) -> bool:
    b = body.strip().lower()
    if not b or b in _PLACEHOLDER_BODIES:
        return True
    if ".." in b:
        return True
    return False


def extract_flag_format(text: str) -> Optional[str]:
    """
    Cố gắng suy ra flag format regex từ văn bản (rules/description).

    Thứ tự ưu tiên:
      (a) Regex tường minh trong văn bản: /^XXX\\{.*\\}$/
      (b) Code span / backtick chứa pattern có {...}: `XXX{...}`
      (c) Fallback: candidate PREFIX{body} xuất hiện gần từ khoá
          "flag" / "format" / "định dạng" (cùng dòng hoặc ±1 dòng),
          chọn prefix phổ biến nhất.

    Trả về regex neo đầy đủ (^PREFIX\\{.+\\}$) hoặc None nếu không chắc chắn.
    """
    if not text or not text.strip():
        return None

    # (a) Regex tường minh
    m = _EXPLICIT_FMT_RE.search(text)
    if m:
        return build_format_regex(m.group(1))

    # (b) Code span chứa pattern {...}
    for span in _CODE_SPAN_RE.findall(text):
        sm = _SPAN_FMT_RE.match(span)
        if sm and not _is_placeholder_body(sm.group(2)):
            return build_format_regex(sm.group(1))

    # (c) Fallback: candidate gần từ khoá
    lines: List[str] = text.splitlines()
    hint_idx = {
        i for i, ln in enumerate(lines)
        if any(w in ln.lower() for w in _FORMAT_HINT_WORDS)
    }
    counter: Counter = Counter()
    for i, ln in enumerate(lines):
        if i not in hint_idx and (i - 1) not in hint_idx and (i + 1) not in hint_idx:
            continue
        for cm in _FLAG_CANDIDATE_RE.finditer(ln):
            prefix, body = cm.group(1), cm.group(2).strip()
            if _is_placeholder_body(body):
                continue
            counter[prefix] += 1

    if counter:
        prefix, _count = counter.most_common(1)[0]
        return build_format_regex(prefix)

    return None


def validate_flag(flag: str, fmt_regex: str) -> bool:
    """
    Kiểm tra flag khớp hoàn toàn với regex định dạng.
    Mọi exception khi compile/match -> False.
    """
    if not flag or not fmt_regex:
        return False
    try:
        return re.fullmatch(fmt_regex, flag.strip()) is not None
    except re.error:
        return False
