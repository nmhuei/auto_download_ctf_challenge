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
        
    return clean[:max_length]

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
