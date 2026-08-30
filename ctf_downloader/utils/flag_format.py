"""
SP1 — Tiện ích nhận diện & kiểm tra định dạng flag.
Các hàm thuần (pure functions), không I/O, dễ test.
"""
import re
import threading
import unicodedata
from collections import Counter
from typing import List, Optional

# --- ReDoS guard ---------------------------------------------------------
# Python re không có timeout native và KHÔNG thể ngắt bằng thread/SIGALRM:
# sre matching chạy hoàn toàn trong C, không nhả GIL cho tới khi xong —
# thread chính join(timeout) vẫn phải đợi match kết thúc. Phòng thủ thực tế:
#   1. Giới hạn độ dài pattern.
#   2. Từ chối pattern có QUANTIFIER LỒNG NHAU ((a+)+$, (.*)*, (x{1,5})+ ...)
#      và ALTERNATION TRÙNG NHÁNH ((a|a)+$, ([ab]|[ab]), (?i)(x|X)+$,
#      (\x61|a)+$ — nhánh tương đương sau khi decode escape/fold inline-flag)
#      — hai lớp catastrophic backtracking kinh điển — bằng phân tích cú
#      pháp nhẹ (scan escape/class/group đúng cách, chuẩn hoá nhánh trước
#      khi so sánh).
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


# Mọi ký tự LITERAL sau chuẩn hoá được encode thành "\x00<hex>;" — marker
# này không bao giờ xuất hiện nguyên trạng ngoài encoder (mọi ký tự thường
# đều đi qua encoder), nhờ đó literal '|' từ ``\|`` không bị nhầm với
# metachar '|' cấu trúc, literal '(' từ ``\(`` không nhầm với dấu mở nhóm.
_LITERAL_MARK = "\x00"
_FLAG_CHARS = set("aiLmsux-")


def _norm_literal(ch: str, icase: bool) -> str:
    """Encode một ký tự literal thành token so sánh an toàn; fold lowercase
    khi IGNORECASE đang hiệu lực ('x' ≡ 'X' dưới (?i))."""
    if icase:
        low = ch.lower()
        if len(low) == 1:
            # U+0130 'İ'.lower() dài 2 ký tự ('i' + U+0307) — codepoint
            # duy nhất trên Unicode; giữ nguyên thay vì để ord() raise
            # TypeError ngoài try/except ở call-site _is_risky_pattern.
            ch = low
    return _LITERAL_MARK + format(ord(ch), "x") + ";"


def _is_flag_segment(seg: str) -> bool:
    """``seg`` có phải bộ inline flags hợp lệ (vd 'i', 'imsx', '-i',
    'im-sx')? Dùng để phân biệt (?i)/(?i: với (?P<name>, (?P=name...)."""
    return (bool(seg)
            and all(c in _FLAG_CHARS for c in seg)
            and any(c.isalpha() for c in seg))


def _norm_escape_token(pattern: str, i: int, n: int, in_class: bool,
                       icase: bool, captures: int):
    """Chuẩn hoá escape bắt đầu tại ``pattern[i] == '\\'`` thành một token so
    sánh. Trả về ``(token, next_i)``:

    - Escape sinh KÝ TỰ LITERAL (\x61, \\u0061, \\U00000061, octal \\0dd,
      \\n \\t ..., \\\\, \\. \\{ ...) -> token đã encode qua ``_norm_literal``
      để ``\\x61`` ≡ ``a``, ``\\|`` ≡ literal '|';
    - Escape SYMBOLIC (\\d \\w \\s \\A \\Z..., \\b ngoài class, backreference
      \\1 \\g<name>, escape lạ) -> giữ nguyên văn — không suy ra nội dung."""
    if i + 1 >= n:
        return "\\", n                      # backslash cụt — re sẽ báo lỗi
    c = pattern[i + 1]
    simple = {"a": "\a", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
    if c in simple:
        return _norm_literal(simple[c], icase), i + 2
    if c == "b":
        if in_class:
            return _norm_literal("\x08", icase), i + 2   # backspace trong class
        return "\\b", i + 2                              # word boundary
    if c in ("d", "w", "s", "D", "W", "S", "B", "A", "Z"):
        return "\\" + c, i + 2
    for kind, width in (("x", 2), ("u", 4), ("U", 8)):
        if c == kind:
            h = pattern[i + 2:i + 2 + width]
            if (len(h) == width
                    and all(d in "0123456789abcdefABCDEF" for d in h)):
                try:
                    ch = chr(int(h, 16))
                except ValueError:
                    return "\\" + kind, i + 2    # ngoài khoảng Unicode hợp lệ
                return _norm_literal(ch, icase), i + 2 + width
            return "\\" + kind, i + 2                # hỏng/không đủ chiều dài
    if c == "N" and i + 2 < n and pattern[i + 2] == "{":
        end = pattern.find("}", i + 3)
        if end != -1:
            try:
                ch = unicodedata.lookup(pattern[i + 3:end])
                return _norm_literal(ch, icase), end + 1
            except KeyError:
                return pattern[i:end + 1], end + 1       # tên không tồn tại
        return "\\N", i + 2
    if c.isdigit():
        if c == "0":                                     # octal \0dd
            k = i + 2
            while k < n and k - (i + 2) < 2 and pattern[k] in "01234567":
                k += 1
            return _norm_literal(chr(int(pattern[i + 1:k], 8)), icase), k
        # \1..\9, \12... : theo luật sre, ĐỦ 3 chữ số octal là octal
        # escape VÔ ĐIỀU KIỆN (docs "\\number": "number is 3 octal digits
        # long" -> không bao giờ là group match) — kể cả khi nhóm tương
        # ứng tồn tại. Luật này phải xét TRƯỚC vòng prefix-backref, nếu
        # không pattern có >=101 nhóm sẽ coi \101 là backref giả và bỏ
        # lỡ dup thật dạng (A|\101)+$.
        k = i + 1
        while k < n and pattern[k].isdigit():
            k += 1
        digits = pattern[i + 1:k]
        if len(digits) == 3 and all(d in "01234567" for d in digits):
            val = int(digits, 8)
            if val <= 0o377:
                return _norm_literal(chr(val), icase), k
            return pattern[i:k], k          # >0o377 — re sẽ báo lỗi range
        ref_len = 0
        for ln in range(len(digits), 0, -1):
            if int(digits[:ln]) <= captures:
                ref_len = ln
                break
        if ref_len:
            return pattern[i:i + 1 + ref_len], i + 1 + ref_len
        if (len(digits) <= 3 and all(d in "01234567" for d in digits)
                and int(digits, 8) <= 0o377):
            return _norm_literal(chr(int(digits, 8)), icase), k
        return pattern[i:k], k          # re sẽ báo invalid group reference
    if c == "g":                                         # \g<name> / \g'name'
        o = pattern[i + 2] if i + 2 < n else ""
        if o in ("'", "<"):
            close = ">" if o == "<" else "'"
            end = pattern.find(close, i + 3)
            if end != -1:
                return pattern[i:end + 1], end + 1
        return "\\g", i + 2
    if not c.isalnum():                                  # \. \( \{ ... -> literal
        return _norm_literal(c, icase), i + 2
    return "\\" + c, i + 2                               # escape lạ — re sẽ từ chối


def _norm_class_token(cls: str, icase: bool, captures: int) -> str:
    """Chuẩn hoá một character class nguyên khối ``[...]``: decode escape về
    literal, fold case theo IGNORECASE; giữ nguyên cấu trúc ``^``, '-' và
    '[' (posix class) để hai class khác ngữ nghĩa không bao giờ hoá vào
    nhau. Hai class giống nhau sau chuẩn hoá thì mới coi là trùng."""
    out = ["["]
    body = cls[1:-1]
    m = len(body)
    k = 0
    if body.startswith("^"):
        out.append("^")
        k = 1
    if k < m and body[k] == "]":
        out.append(_norm_literal("]", icase))   # []] — ']' literal mở màn
        k += 1
    while k < m:
        ch = body[k]
        if ch == "\\":
            tok, adv = _norm_escape_token(body, k, m, True, icase, captures)
            out.append(tok)
            k = adv
            continue
        if ch in "-[":
            out.append(ch)
        else:
            out.append(_norm_literal(ch, icase))
        k += 1
    out.append("]")
    return "".join(out)


def _scan_dup_alternation(pattern: str) -> bool:
    """True nếu pattern có nhóm alternation chứa HAI NHÁNH TƯƠNG ĐƯƠNG nhau
    (vd ``(a|a)+``, ``([ab]|[ab])``, ``(?i)(x|X)+$``, ``(\\x61|a)+$``). Nhánh
    trùng nhân đôi số đường backtracking theo cấp số mũ khi đứng dưới
    quantifier — biến thể ReDoS mà _scan_nested_quantifier không thấy vì
    không có quantifier nào lồng quantifier nào ((a|a)+$ vẫn exponential).

    Heuristic quét một lượt O(n), KHÔNG phải regex-analyzer hoàn chỉnh.
    Trước khi so sánh, token được CHUẨN HOÁ về dạng tương đương để chống
    bypass qua chính tả:
      - decode escape sinh literal (\\x61, \\u0061, octal, \\n, \\\\ ...) về
        ký tự thường -> ``\\x61`` ≡ ``a``;
      - theo dõi inline flags: (?i) toàn cục và (?i:...) phạm vi nhóm bật
        case-fold cho phần phía sau -> ``(?i)(x|X)`` bị thấy là trùng
        (flag không đổi nội dung nhánh nhưng đổi NGÔN NGỮ so sánh);
      - thân nhóm con đóng được đưa vào khung cha ở dạng ĐÃ chuẩn hoá,
        nên dup lồng kiểu ``((\\x61)|a)+`` vẫn bị bắt;
      - literal bọc marker ``\\x00<hex>;`` để không đụng độ metachar cấu
        trúc giữ nguyên văn (``\\|`` là literal '|' và phải khác '|').

    Giới hạn còn lại (chấp nhận miss có chủ ý): backreference \\1/\\g<name>
    giữ nguyên văn; các cặp tương đương hình thức khác (\\d vs [0-9],
    [a-b] vs [ab], hai class khác chính tả dưới (?i)) và ngữ nghĩa phức
    tạp hơn (recursive, khoảng cách lặp đếm lớn) KHÔNG được mô hình hoá."""
    n = len(pattern)
    i = 0
    in_class = False
    class_start = -1
    icase = False   # IGNORECASE hiệu lực tại vị trí đang quét
    captures = 0    # số nhóm capturing đã mở — quyết định \NN = backref/octal
    # Mỗi khung nhóm: buffer nhánh đang gom + tập nhánh đã thấy ở mức đó +
    # toàn thân nhóm đã chuẩn hoá ("full", làm nguyên tử cho khung cha) +
    # snapshot icase lúc mở nhóm (để kết thúc phạm vi (?i:...).
    stack: List[dict] = [
        {"buf": [], "full": [], "seen": set(), "start": 0, "icase": False}
    ]

    def _push(frame: dict, token: str) -> None:
        frame["buf"].append(token)
        frame["full"].append(token)

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
                tok = _norm_class_token(pattern[class_start:i + 1], icase,
                                        captures)
                _push(stack[-1], tok)
                in_class = False
            i += 1
            continue
        if c == "\\":
            tok, i = _norm_escape_token(pattern, i, n, False, icase, captures)
            _push(stack[-1], tok)
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
            # Inline flags: (?flags) toàn cục hoặc (?flags:body) phạm vi nhóm.
            flag_end = -1
            scoped = False
            if i + 1 < n and pattern[i + 1] == "?":
                j = i + 2
                while j < n and (pattern[j].isalpha() or pattern[j] == "-"):
                    j += 1
                seg = pattern[i + 2:j]
                if j < n and pattern[j] in ":)" and _is_flag_segment(seg):
                    negated = False
                    for fch in seg:
                        if fch == "-":
                            negated = True
                        elif fch == "i":
                            icase = not negated
                    scoped = pattern[j] == ":"
                    flag_end = j + 1
            if flag_end != -1:
                if scoped:
                    # (?flags:body) — snapshot icase sau khi áp flags; khôi
                    # phục khi nhóm đóng.
                    stack.append({"buf": [], "full": [], "seen": set(),
                                  "start": i, "icase": icase})
                i = flag_end          # (?flags) toàn cục: không mở nhóm mới
                continue
            # Đánh số nhóm capturing: (...) và (?P<name> / (?<name> có số;
            # (?:, (?=, (?!, (?<=, (?<!, (?flags: không.
            if i + 1 >= n or pattern[i + 1] != "?":
                captures += 1                       # nhóm thường (...)
            elif i + 3 < n and pattern[i + 2] == "P" and pattern[i + 3] == "<":
                captures += 1                       # (?P<name>
            elif i + 3 < n and pattern[i + 2] == "<" and pattern[i + 3] not in ("=", "!"):
                captures += 1                       # (?<name>
            stack.append({"buf": [], "full": [], "seen": set(),
                          "start": i, "icase": icase})
            # Nhảy qua intro nhóm (?:, (?=, (?P<name>, ...) — nếu không,
            # nhánh đầu sẽ bị dính chữ khai báo và so nguyên văn sai lệch.
            i = _group_body_start(pattern, i)
            continue
        if c == ")":
            if len(stack) > 1:
                frame = stack.pop()
                if _close_branch(frame):
                    return True
                icase = frame["icase"]     # hết phạm vi (?flags:...)
                # Nhóm đóng -> ghi THÂN nhóm ĐÃ CHUẨN HOÁ vào khung cha để
                # so đúng ngữ nghĩa: ``(?:ab)`` ≡ ``(ab)`` ≡ ``ab`` trong
                # nhánh, và ``((\\x61)|a)`` ≡ trùng sau decode.
                atom = "".join(frame["full"])
                parent = stack[-1]
                parent["buf"].append(atom)
                parent["full"].append(atom)
            i += 1
            continue
        if c == "|":
            if _close_branch(stack[-1]):
                return True
            stack[-1]["full"].append("|")
            i += 1
            continue
        _push(stack[-1], _norm_literal(c, icase))
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
