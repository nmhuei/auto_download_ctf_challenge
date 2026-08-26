import re
import urllib.parse
from typing import Optional

import unicodedata

def remove_accents(input_str: str) -> str:
    """
    Transliterates Vietnamese and Unicode accents to standard ASCII.
    """
    if not input_str:
        return ""
    # Vietnamese specific replacements for đ/Đ
    s = input_str.replace("đ", "d").replace("Đ", "D")
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join([c for c in nfkd if not unicodedata.combining(c)])

def sanitize_ctf_title(title: str, fallback_domain: str = "") -> str:
    """
    Creates a clean, normalized CTF folder name (e.g. 'PTIT_CTF_2026').
    """
    if not title and fallback_domain:
        title = fallback_domain.replace("https://", "").replace("http://", "").split("/")[0].replace(".", "_")

    if not title:
        return "CTF_Workspace"

    ascii_title = remove_accents(title)
    clean = re.sub(r'[^a-zA-Z0-9_\-]+', '_', ascii_title)
    clean = re.sub(r'_+', '_', clean).strip('_')
    return clean or "CTF_Workspace"

# ECMA-48 escape: CSI ``ESC [ ... final`` + OSC ``ESC ] ... BEL/ST``.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Ký tự điều khiển còn lại (bao cả \n \r \t) — không bao giờ hợp lệ trong
# tên team/category do server trả về.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def strip_ansi(value) -> str:
    """Gỡ ESC sequence và ký tự điều khiển khỏi dữ liệu SERVER kiểm soát
    (tên team/title scoreboard). Không gỡ thì terminal injection đi nguyên
    vào output rich Text (\x1b[31m đổi màu, OSC đổi title terminal...).
    None -> chuỗi rỗng; kiểu khác được ép str."""
    text = str(value) if value is not None else ""
    text = _ANSI_CSI_RE.sub("", text)
    text = _ANSI_OSC_RE.sub("", text)
    return _CTRL_RE.sub("", text)


# review-5 follow-up: MỘT bảng escape markdown dùng chung cho mọi đường
# nhúng dữ liệu ngoài (tên challenge/team/category...) vào file .md —
# trước đây writeup_exporter._md_escape và sanitize.md_cell mỗi bên giữ
# một chiến lược escape song song.
_MD_SPECIALS = "\\`*_[]|"


def escape_markdown(value, chars=_MD_SPECIALS) -> str:
    r"""Backslash-escape các ký tự markdown đặc biệt của ``value`` — chống
    markdown injection: tên chứa ``[bold]`` / ``[x](http://evil)`` không
    sinh format/link ngoài ý muốn khi render.

    ``chars`` cho phép caller thu hẹp tập escape: md_cell chỉ cần ``[]``
    (``|`` xử lý riêng bằng thực thể HTML vì backslash-escape KHÔNG đủ
    trong ô bảng GFM), đường INDEX.md của writeup_exporter dùng trọn bộ
    mặc định. None -> chuỗi rỗng; kiểu khác được ép str."""
    text = str(value) if value is not None else ""
    if not text or not chars:
        return text
    return re.sub("[" + re.escape(chars) + "]", lambda m: "\\" + m.group(0),
                  text)


def md_cell(value) -> str:
    r"""Sanitize một giá trị dữ liệu ngoài để nhúng vào MỘT ô của bảng
    markdown (RANKING.md / SUMMARY.md):
      - gập \r\n/\r/\n thành khoảng trắng TRƯỚC — newline sinh hàng bảng
        giả (strip_ansi đứng sau sẽ chỉ gỡ control còn lại);
      - strip_ansi: ESC không được vào file .md;
      - thay ``|`` bằng thực thể HTML &#124; — pipe sinh cột ảo vỡ bảng.
    Backslash-escape (``\|``) KHÔNG đủ: pipe vẫn còn trong text thô nên bộ
    đếm cell vẫn thấy bảng vỡ.
      - escape ``[``/``]`` qua BẢNG ESCAPE CHUNG :func:`escape_markdown`
        (review-5): ngoặc vuông trong link-text ``[tên](path)`` vỡ cấu trúc
        link / markdown injection.
    Văn bản sạch đi qua nguyên vẹn (no-op)."""
    text = str(value) if value is not None else ""
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = strip_ansi(text)
    # Escape SAU khi pipe đã thành thực thể — entity không chứa ký tự đặc
    # biệt nên không bị escape đè.
    return escape_markdown(text.replace("|", "&#124;"), chars="[]")


def sanitize_folder_name(name: str, max_length: int = 80, default: str = "challenge") -> str:
    """
    Sanitize challenge or category name to be safe across Linux, macOS, and Windows.
    """
    if not name or not isinstance(name, str):
        return default
    
    # Strip whitespace
    name = name.strip()
    
    # Replace illegal filesystem characters: / \ : * ? " < > | \0
    clean = re.sub(r'[\\/*?:"<>|\x00-\x1f]', '_', name)
    
    # Replace consecutive spaces or underscores
    clean = re.sub(r'[\s_]+', '_', clean)
    
    # Remove leading/trailing dots, underscores, or spaces (Windows issues)
    clean = clean.strip(' ._')
    
    if not clean:
        return default

    clean = clean[:max_length]

    # C9-02: NAME_MAX của Linux là 255 BYTE UTF-8 chứ không phải số ký tự
    # (80 emoji = 320 byte -> os.makedirs OSError, challenge rơi khỏi
    # workspace). Ép trần 254 byte; phần cắt rơi giữa multi-byte sequence
    # bị bỏ qua khi decode thay vì tạo byte lỗi.
    return clean.encode("utf-8")[:254].decode("utf-8", "ignore")


def sanitize_filename(name: str, max_length: int = 120, default: str = "attachment.bin") -> str:
    """
    Sanitize filename while preserving file extension if possible.

    C19-M6: ``max_length`` tính theo BYTE utf-8 (NAME_MAX của Linux là 255
    byte, không phải số ký tự — tên emoji dài cắt theo char vẫn có thể vượt
    trần và OSError lúc ghi). Phần cắt rơi giữa multi-byte sequence bị bỏ
    qua khi decode thay vì sinh byte lỗi / đứt codepoint; nếu cắt xong rỗng
    thì trả ``default``.
    """
    if not name or not isinstance(name, str):
        return default

    name = urllib.parse.unquote(name).strip()

    # Extract query params if attached to filename (e.g. file.zip?token=xxx)
    if '?' in name:
        name = name.split('?')[0]
    if '#' in name:
        name = name.split('#')[0]

    # Replace illegal filesystem characters
    clean = re.sub(r'[\\/*?:"<>|\x00-\x1f]', '_', name)
    clean = clean.strip(' .')

    if not clean:
        return default

    raw = clean.encode("utf-8")
    if len(raw) > max_length:
        clean = raw[:max_length].decode("utf-8", "ignore")
    return clean or default

def extract_filename_from_url(url: str, default: str = "download.bin") -> str:
    """
    Extracts filename from URL path.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        path = parsed.path
        filename = path.rstrip('/').split('/')[-1]
        if filename:
            return sanitize_filename(filename, default=default)
    except Exception:
        pass
    return default

def extract_filename_from_headers(headers: dict, fallback_url: str = "") -> str:
    """
    Extracts filename from Content-Disposition response header or falls back to URL.
    """
    cd = headers.get("Content-Disposition") or headers.get("content-disposition")
    if cd:
        # Check for filename*=UTF-8''filename.ext (RFC 5987)
        match_utf8 = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", cd, re.IGNORECASE)
        if match_utf8:
            return sanitize_filename(urllib.parse.unquote(match_utf8.group(1)))
            
        # Check for filename="filename.ext" or filename=filename.ext
        match = re.search(r'filename\s*=\s*(?:"([^"]+)"|([^;\s]+))', cd, re.IGNORECASE)
        if match:
            fn = match.group(1) if match.group(1) is not None else match.group(2)
            return sanitize_filename(fn)
            
    if fallback_url:
        return extract_filename_from_url(fallback_url)
    return "downloaded_file"
