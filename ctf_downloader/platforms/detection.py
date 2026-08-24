"""
SP3 — Recon & capability map (pipeline 4 tầng, registry-driven).

Pipeline nhận diện nền tảng 4 tầng (rẻ -> đắt, dừng khi chắc chắn):
  1. HTML markers  : GET base_url một lần, quét chuỗi/regex đặc trưng
                     (nguồn: PlatformSpec.html_markers trong registry).
  2. Cookies       : tên cookie đặc trưng (nguồn: PlatformSpec.cookie_hints).
  3. Path probe    : hàm probe API (nguồn: PlatformSpec.probes), cũng dùng để
                     làm giàu capabilities ngay cả khi tầng 1 đã nhận diện.
  4. Fallback      : hành vi cũ (chuỗi URL, custom REST) -> cuối cùng là
                     GenericHTMLPlatform thay vì CTFdPlatform (fix bug fallback).

Chỉ THỨ TỰ ưu tiên giữa các tầng là chính sách của pipeline; toàn bộ dữ liệu
nhận diện (markers / cookie_hints / probes / label) đọc từ platforms.registry.

API chính:
  - detect_platform_info(url, session, cookie_hint=None) -> (platform_instance, PlatformInfo)
  - detect_platform(url, session) -> platform_instance (tương thích ngược 100%,
    platform instance được gán sẵn thuộc tính `.info` = PlatformInfo).
"""

import re
from typing import Optional, Tuple

from .base import BasePlatform, safe_get, safe_get_json
from .capabilities import PlatformInfo
from .registry import PLATFORMS, get_spec
from ..utils.logger import Logger
from ..utils.urlnorm import parse_normalized

# --------------------------------------------------------------------------- #
# Chính sách thứ tự ưu tiên (giữ nguyên hành vi pipeline cũ) — dữ liệu từ registry
# --------------------------------------------------------------------------- #
_MARKER_PRIORITY = ("rctf", "ctfd", "gzctf")
_COOKIE_PRIORITY = ("gzctf", "ctfd")
_PROBE_PRIORITY = ("gzctf", "ctfd", "rctf")

# Thông điệp signal tầng 1 theo platform key (giữ nguyên văn bản cũ để
# tương thích với các test/log hiện có)
_MARKER_SIGNALS = {
    "rctf": "HTML marker: <meta name=\"rctf-config\"> hoặc envelope {kind,message,data}",
    "ctfd": "HTML marker: csrfNonce' / window.init / Powered by CTFd / themes/core",
    "gzctf": "HTML marker: <meta keywords> GZCTF hoặc chuỗi GZCTF/GZ::CTF",
}

# Thông điệp signal tầng 2
_COOKIE_SIGNALS = {
    "gzctf": "Cookie GZCTF_Token trong cookie jar/hint -> nghi GZ::CTF",
    "ctfd": "Cookie Flask 'session' vừa được set -> nghi CTFd",
}


def _match_html_markers(spec, html: str, low: str) -> bool:
    """Khớp một marker của spec trên HTML. Marker tiền tố 'regex:' là mẫu regex."""
    for marker in spec.html_markers:
        if marker.startswith("regex:"):
            try:
                if re.search(marker[len("regex:"):], html):
                    return True
            except re.error:
                continue
        elif marker.lower() in low:
            return True
    return False


def detect_platform_info(base_url: str, session,
                         cookie_hint: Optional[str] = None,
                         quiet: bool = False) -> Tuple[BasePlatform, PlatformInfo]:
    """
    Dò tìm nền tảng CTF theo pipeline 4 tầng, trả về platform instance
    (tương thích hoàn toàn với chữ ký/cách dùng cũ) kèm PlatformInfo.

    ``quiet=True``: tắt toàn bộ log Logger của pipeline nhận diện (dòng
    ``[*] Detected Platform`` 16-color và warning fallback) — dành cho các
    surface tự render report riêng theo design system (vd ``ctf doctor``),
    tránh lẫn rainbow/default-style vào output PHOSPHOR.
    """
    parsed, origin, clean_base_url = parse_normalized(base_url)
    info = PlatformInfo(platform_type="unknown", base_url=clean_base_url)

    game_match = re.search(r"/games?/(\d+)", parsed.path)
    if game_match:
        info.game_id = int(game_match.group(1))
        info.add_signal(f"URL chứa /games/{info.game_id} -> game_id={info.game_id}")

    done: set = set()
    ptype, confidence = "unknown", "low"

    # ---------------- Tầng 1: HTML markers (registry) ---------------- #
    html = ""
    resp = safe_get(session, clean_base_url)
    if resp is not None and getattr(resp, "status_code", 0) == 200:
        html = getattr(resp, "text", "") or ""

    if html:
        low = html.lower()
        for key in _MARKER_PRIORITY:
            if key not in PLATFORMS:
                continue
            if _match_html_markers(PLATFORMS[key], html, low):
                ptype, confidence = key, "high"
                info.add_signal(_MARKER_SIGNALS.get(key, f"HTML marker khớp {PLATFORMS[key].label}"))
                break
        else:
            info.add_signal("HTML gốc không chứa marker nhận diện nào")

    # ---------------- Tầng 2: Cookies (registry) ---------------- #
    if confidence != "high":
        try:
            cookie_names = set(session.cookies.keys())
        except Exception:
            cookie_names = set()

        matched_cookie = False
        for key in _COOKIE_PRIORITY:
            if key not in PLATFORMS:
                continue
            for hint in PLATFORMS[key].cookie_hints:
                if hint in cookie_names or (
                        cookie_hint and hint.lower() in cookie_hint.lower()):
                    ptype, confidence = key, "medium"
                    info.add_signal(_COOKIE_SIGNALS.get(
                        key, f"Cookie {hint} trong cookie jar/hint -> nghi {PLATFORMS[key].label}"))
                    matched_cookie = True
                    break
            if matched_cookie:
                break

        if not matched_cookie and "_xsrf" in cookie_names:
            # RootTheBox dùng cookie _xsrf nhưng chưa có adapter riêng
            info.add_signal("Cookie _xsrf -> nghi RootTheBox (chưa có adapter)")

    # ------------- Tầng 3: Path probe + envelope (registry) ------------- #
    # Chạy đủ chuỗi probe (theo thứ tự rẻ -> chắc chắn): vừa xác nhận ứng
    # viên ở tầng 2, vừa làm giàu capabilities khi tầng 1 đã nhận diện xong.
    for candidate in _PROBE_PRIORITY:
        spec = PLATFORMS.get(candidate)
        if spec is None:
            continue
        matched = False
        for probe in spec.probes:
            if probe(origin, session, info, done):
                matched = True
                break
        if matched:
            if confidence != "high":
                ptype, confidence = candidate, "high"
            break

    # ------------- Tầng 4: Fallback hành vi cũ ------------- #
    if confidence != "high":
        # Hành vi cũ: Custom REST / Next.js (/api/challenges, /api/auth/me)
        data, status = safe_get_json(session, f"{origin}/api/challenges",
                                     statuses=(200, 401, 403))
        payload = data.get("data") if isinstance(data, dict) else None
        if isinstance(data, dict) and data.get("success") and isinstance(payload, dict) \
                and "challenges" in payload:
            ptype, confidence = "custom_rest", "high"
            info.add_signal(f"GET /api/challenges -> shape Custom REST (HTTP {status})")
        else:
            data, status = safe_get_json(session, f"{origin}/api/auth/me",
                                         statuses=(200,))
            user_data = data.get("data") if isinstance(data, dict) else None
            if isinstance(data, dict) and data.get("success") \
                    and isinstance(user_data, dict) and user_data.get("user"):
                ptype, confidence = "custom_rest", "high"
                info.add_signal(f"GET /api/auth/me -> có user (HTTP {status})")

        if confidence != "high" and "/games" in parsed.path:
            ptype, confidence = "gzctf", "medium"
            info.add_signal("URL chứa /games -> GZ::CTF (nhận diện qua URL, hành vi cũ)")

    # ---------------- Kết luận + dựng platform ---------------- #
    if ptype == "unknown":
        info.add_signal("Fallback: mọi tầng nhận diện thất bại -> generic HTML scraper")
        ptype = "generic_html"
        info.capabilities["scoreboard"] = False  # scraper HTML không có scoreboard API
        if not quiet:
            Logger.warning("Không xác định được nền tảng. Fallback sang GenericHTMLPlatform.")

    spec = get_spec(ptype)
    platform = spec.cls(clean_base_url, session)
    info.platform_type = getattr(platform.ctf_info, "platform_type", ptype)
    platform_game_id = getattr(platform, "game_id", None)
    if isinstance(platform_game_id, int):
        info.game_id = platform_game_id
    info.confidence = confidence

    # setattr mềm: các class platform không cần khai báo sẵn thuộc tính info
    platform.info = info

    if not quiet:
        Logger.info(
            f"Detected Platform: [bold green]{spec.label}[/bold green] "
            f"(confidence: [bold yellow]{confidence}[/bold yellow])"
        )
    return platform, info


def detect_platform(base_url: str, session) -> BasePlatform:
    """
    Auto-detects the CTF platform (CTFd, rCTF, GZCTF, Custom REST, Generic).
    Wrapper tương thích quanh detect_platform_info(): instance trả về luôn
    mang thuộc tính `.info` (PlatformInfo) để caller mới tận dụng recon.
    """
    platform, _info = detect_platform_info(base_url, session)
    return platform
