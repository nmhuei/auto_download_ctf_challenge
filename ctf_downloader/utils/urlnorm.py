"""
Chuẩn hoá URL nền tảng CTF — hợp nhất toàn bộ logic suffix-stripping
(trước đây rải rác trong detector._normalize và config.validate).
"""

import urllib.parse

# Suffix path thường gặp trên trang CTF -> lược bỏ để lấy base URL gốc
URL_PATH_SUFFIXES = ("/challenges", "/scoreboard", "/login", "/register",
                     "/users", "/teams", "/rules", "/notifications")


def parse_normalized(url: str):
    """
    Chuẩn hoá URL: lược fragment/suffix phổ biến.
    Trả (parsed, origin, clean_base_url).
    """
    base_url = (url or "").split("#")[0].rstrip("/")
    parsed = urllib.parse.urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    path = parsed.path.rstrip("/")
    for suffix in URL_PATH_SUFFIXES:
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    clean_base_url = f"{origin}{path}".rstrip("/") or origin
    return parsed, origin, clean_base_url


def normalize_base_url(url: str) -> str:
    """Trả về base URL đã chuẩn hoá (lược fragment + suffix + dấu / cuối)."""
    return parse_normalized(url)[2]
