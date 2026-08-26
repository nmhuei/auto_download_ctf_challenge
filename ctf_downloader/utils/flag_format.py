"""
SP1 — Tiện ích nhận diện & kiểm tra định dạng flag.
Các hàm thuần (pure functions), không I/O, dễ test.
"""
import re
import threading
from collections import Counter
from typing import List, Optional

# --- ReDoS guard ---------------------------------------------------------
# Python re không có timeout native và KHÔNG thể ngắt bằng thread/SIGALRM:
# sre matching chạy hoàn toàn trong C, không nhả GIL cho tới khi xong —
# thread chính join(timeout) vẫn phải đợi match kết thúc. Phòng thủ thực tế:
#   1. Giới hạn độ dài pattern.
#   2. Từ chối pattern có QUANTIFIER LỒNG NHAU ((a+)+$, (.*)*, (x{1,5})+ ...)
#      và ALTERNATION TRÙNG NHÁNH ((a|a)+$, ([ab]|[ab]) — cùng exponential)
#      — hai lớp catastrophic backtracking kinh điển — bằng phân tích cú
#      pháp nhẹ (scan escape/class/group đúng cách).
#   3. Vẫn chạy match trong daemon thread với join timeout làm lớp phòng
#      thủ thứ cấp (pattern chậm do lý do khác -> trả False sau timeout;
#      daemon để process thoát được dù worker còn kẹt).
MAX_PATTERN_LENGTH = 500
MATCH_TIMEOUT_SECONDS = 2.0

_warned_patterns = set()


def _log_redos_warning(pattern: str) -> None:
    if pattern in _warned_patterns:
        return
    _warned_patterns.add(pattern)
    try:
        from .logger import Logger
        Logger.warning(
            f"Flag format quá phức tạp hoặc quá dài (> {MAX_PATTERN_LENGTH} ký tự) "
            f"— bỏ qua kiểm tra format cho pattern này."
        )
    except Exception:
        pass


def _scan_nested_quantifier(pattern: str) -> bool:
    """True nếu pattern chứa quantifier áp lên nhóm có quantifier bên trong
    (vd ``(a+)+``, ``(.*)*{2,}``, ``(?:\\d+)+``) — nguồn catastrophic
    backtracking phổ biến nhất. Scan bỏ qua escape \\x, character class
    [...] và đếm đúng nhóm lồng nhau."""
    n = len(pattern)
    i = 0
    in_class = False
    group_stack = [False]   # đáy = toàn pattern

    def _bounded_repeat(j):
        """Kiểm tra {m,n} bắt đầu tại j; trả về index cuối nếu phải."""
        if j >= n or pattern[j] != "{":
            return -1
        end = pattern.find("}", j)
        if end == -1:
            return -1
        if re.fullmatch(r"\{\d+(,\d*)?\}", pattern[j:end + 1]):
            return end
        return -1

    while i < n:
        c = pattern[i]
        if in_class:
            if c == "\\":
                i += 2
                continue
            if c == "]":
                in_class = False
            i += 1
            continue
        if c == "\\":
            i += 2
            continue
        if c == "[":
            in_class = True
            i += 1
            continue
        if c == "(":
            group_stack.append(False)
            i += 1
            continue
        if c == ")":
            inner_had_q = group_stack.pop() if len(group_stack) > 1 else False
            # quantifier ngay sau dấu )?
            j = i + 1
            quantified = False
            if j < n and pattern[j] in "*+":
                quantified = True
            else:
                e = _bounded_repeat(j)
                if e != -1:
                    quantified = True
            if quantified and inner_had_q:
                return True
            if (quantified or inner_had_q) and group_stack:
                group_stack[-1] = group_stack[-1] or (inner_had_q and not quantified) or quantified
            i += 1
            continue
        if c in "*+":
            group_stack[-1] = True
            i += 1
            continue
        if c == "?":
            # '??'/'*?'/'+?' là lazy-modifier của quantifier đứng trước,
            # bản thân '?' (optional) cũng là quantifier.
            group_stack[-1] = True
            i += 1
            continue
        e = _bounded_repeat(i)
        if e != -1:
            group_stack[-1] = True
            i = e + 1
            continue
        i += 1
    return False


def _scan_dup_alternation(pattern: str) -> bool:
    """True nếu pattern có nhóm alternation chứa HAI NHÁNH GIỐNG HỆT nhau
    (vd ``(a|a)+``, ``([ab]|[ab])``, ``((x|y)|(x|y))``). Nhánh trùng nhân
    đôi số đường backtracking theo cấp số mũ khi đứng dưới quantifier —
    biến thể ReDoS mà _scan_nested_quantifier không thấy vì không có
    quantifier nào lồng quantifier nào ((a|a)+$ vẫn exponential).

    Heuristic thuần văn bản (KHÔNG phải regex-analyzer hoàn chỉnh): quét
    một lượt O(n), bỏ qua đúng escape \\x và character class [...], so sánh
    NHÁNH NGUYÊN VĂN trong cùng một mức nhóm; mỗi nhóm đóng được ghi nhận
    như một "nguyên tử" nguyên văn của khung cha để bắt cả dup lồng nhau."""
    n = len(pattern)
    i = 0
    in_class = False
    class_start = -1
    # Mỗi khung nhóm: buffer nhánh đang gom + tập nhánh đã thấy ở mức đó.
    stack: List[dict] = [{"buf": [], "seen": set(), "start": 0}]

    def _close_branch(frame: dict) -> bool:
        branch = "".join(frame["buf"])
        frame["buf"] = []
        if not branch:
            return False          # nhánh rỗng (`(a|)`) bỏ qua
        if branch in frame["seen"]:
            return True
        frame["seen"].add(branch)
        return False

    while i < n:
        c = pattern[i]
        if in_class:
            if c == "\\":
                i += 2
                continue
            if c == "]":
                # Cả class giữ nguyên văn như một nguyên tử của nhánh.
                stack[-1]["buf"].append(pattern[class_start:i + 1])
                in_class = False
            i += 1
            continue
        if c == "\\":
            stack[-1]["buf"].append(pattern[i:i + 2])
            i += 2
            continue
        if c == "[":
            in_class = True
            class_start = i
            i += 1
            continue
        if c == "(":
            if i + 2 < n and pattern[i + 1] == "?" and pattern[i + 2] == "#":
                # (?#comment) — không phải nhóm, bỏ qua trọn comment.
                end = pattern.find(")", i + 3)
                i = (end + 1) if end != -1 else n
                continue
            stack.append({"buf": [], "seen": set(), "start": i})
            # Nhảy qua intro nhóm (?:, (?=, (?P<name>, (?i:, ...) — nếu không,
            # nhánh đầu sẽ bị dính chữ khai báo và so nguyên văn sai lệch.
            i = _group_body_start(pattern, i)
            continue
        if c == ")":
            if len(stack) > 1:
                frame = stack.pop()
                if _close_branch(frame):
                    return True
                # Nhóm đóng -> ghi THÂN nhóm (đã bỏ intro và cặp ngoặc)
                # vào khung cha để so nguyên văn đúng ngữ nghĩa:
                # ``(?:ab)`` ≡ ``(ab)`` ≡ ``ab`` khi đứng trong nhánh.
                body_start = _group_body_start(pattern, frame["start"])
                stack[-1]["buf"].append(pattern[body_start:i])
            i += 1
            continue
        if c == "|":
            if _close_branch(stack[-1]):
                return True
            i += 1
            continue
        stack[-1]["buf"].append(c)
        i += 1
    return False


def _is_risky_pattern(pattern: str) -> bool:
    """Gate chung cho mọi bề mặt regex nhận pattern untrusted: rỗng / quá
    dài / quantifier lồng nhau / alternation trùng nhánh -> từ chối TRƯỚC
    khi chạm re (sre chạy trong C không nhả GIL, timeout thread không cứu
    được một match catastrophic — lớp tĩnh này là phòng thủ chính)."""
    return (not pattern
            or len(pattern) > MAX_PATTERN_LENGTH
            or _scan_nested_quantifier(pattern)
            or _scan_dup_alternation(pattern))


def _group_body_start(pattern: str, open_idx: int) -> int:
    """Index ký tự đầu của THÂN nhóm mở tại ``open_idx`` — nhảy qua các
    intro (?:, (?=, (?!, (?<=, (?<!, (?P<name>, (?<name>, (?flags:) để
    nhánh alternation không bị dính chữ khai báo khi so nguyên văn."""
    n = len(pattern)
    j = open_idx + 1
    if j >= n or pattern[j] != "?":
        return j                       # nhóm thường (...)
    k = j + 1
    if k >= n:
        return n
    if pattern[k] == "<":
        if k + 1 < n and pattern[k + 1] in ("=", "!"):
            return k + 3               # (?<=  /  (?<!
        gt = pattern.find(">", k + 1)
        return (gt + 1) if gt != -1 else n      # (?<name>
    if pattern[k] == "P" and k + 1 < n and pattern[k + 1] == "<":
        gt = pattern.find(">", k + 2)
        return (gt + 1) if gt != -1 else n      # (?P<name>
    # ?: ?= ?! ?~ và (?flags: — ăn hết [A-Za-z-]* rồi nếu gặp ':' thì qua.
    m = k
    while m < n and (pattern[m].isalpha() or pattern[m] == "-"):
        m += 1
    if m < n and pattern[m] == ":":
        return m + 1                # (?i:body / (?im-sx:body
    return k + 1                    # ?: ?= ?! ?~ — thân bắt đầu sau intro


def _regex_with_timeout(fn, timeout: float = MATCH_TIMEOUT_SECONDS):
    """Chạy ``fn()`` trong daemon thread, chờ tối đa ``timeout`` giây.

    Trả về kết quả của fn(); nếu fn raise hoặc timeout -> trả None.
    LƯU Ý: với re thuần, timeout này không ngắt được match đang kẹt trong
    C (sre không nhả GIL) — lớp phòng thủ chính là _scan_nested_quantifier.
    """
    box = {}

    def _runner():
        try:
            box["value"] = fn()
        except re.error:
            box["value"] = None
        except Exception:
            box["value"] = None

    t = threading.Thread(target=_runner, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None   # timeout — caller quyết định nghĩa của None
    return box.get("value")



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


def regex_search_with_timeout(pattern: str, text: str, timeout: float = MATCH_TIMEOUT_SECONDS):
    """re.search có ReDoS guard: trả match, hoặc None nếu không khớp /
    pattern hỏng / quá phức tạp (nested quantifier, alternation trùng nhánh).
    Dùng chung cho các bề mặt chạy regex user-supplied (validate_flag,
    writeup_assessor)."""
    if _is_risky_pattern(pattern):
        _log_redos_warning(pattern or "")
        return None
    return _regex_with_timeout(lambda: re.search(pattern, text), timeout)


def regex_matches_with_timeout(
    pattern: str,
    text: str,
    limit: int = 100,
    timeout: float = MATCH_TIMEOUT_SECONDS,
):
    """re.finditer có ReDoS guard: trả LIST các match (tối đa ``limit``),
    hoặc None nếu pattern hỏng / quá dài / nested quantifier / alternation
    trùng nhánh / timeout."""
    if _is_risky_pattern(pattern):
        _log_redos_warning(pattern or "")
        return None

    def _find_all():
        out = []
        for m in re.finditer(pattern, text):
            out.append(m)
            if len(out) >= limit:
                break
        return out

    return _regex_with_timeout(_find_all, timeout)


def validate_flag(flag: str, fmt_regex: str) -> bool:
    """
    Kiểm tra flag khớp hoàn toàn với regex định dạng.
    Mọi exception khi compile/match -> False.
    Pattern quá dài, quantifier lồng nhau hoặc alternation trùng nhánh
    (ReDoS) -> False + warning.
    """
    if not flag or not fmt_regex:
        return False
    if _is_risky_pattern(fmt_regex):
        _log_redos_warning(fmt_regex)
        return False

    stripped = flag.strip()

    def _match():
        return re.fullmatch(fmt_regex, stripped) is not None

    result = _regex_with_timeout(_match)
    if result is None:
        # re.error đã bị nuốt trong _runner; None còn lại nghĩa là timeout
        # (pattern chậm bất thường) -> coi như không khớp.
        _log_redos_warning(fmt_regex)
        return False
    return result
