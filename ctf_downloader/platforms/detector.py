"""
SP3 — Recon & capability map.

Pipeline nhận diện nền tảng 4 tầng (rẻ -> đắt, dừng khi chắc chắn):
  1. HTML markers  : GET base_url một lần, quét meta tag / chuỗi đặc trưng.
  2. Cookies       : GZCTF_Token, Flask session, _xsrf (RootTheBox).
  3. Path probe    : /api/config (GZCTF), /api/game/recent (GZCTF),
                     /api/v1/challenges (CTFd), /api/v1/challs (rCTF envelope).
  4. Fallback      : hành vi cũ (chuỗi URL, custom REST) -> cuối cùng là
                     GenericHTMLPlatform thay vì CTFdPlatform (fix bug fallback).

API chính:
  - detect_platform_info(url, session, cookie_hint=None) -> (platform_instance, PlatformInfo)
  - detect_platform(url, session) -> platform_instance (tương thích ngược 100%,
    platform instance được gán sẵn thuộc tính `.info` = PlatformInfo).
"""

import json
import re
import urllib.parse
import requests
from typing import Optional, Tuple
from .base import BasePlatform
from .ctfd import CTFdPlatform
from .rctf import RCTFPlatform
from .gzctf import GZCTFPlatform
from .custom_rest import CustomRESTPlatform
from .generic_html import GenericHTMLPlatform
from .capabilities import PlatformInfo
from ..utils.logger import Logger

# Suffix path thường gặp trên trang CTF -> lược bỏ để lấy base URL gốc
_URL_PATH_SUFFIXES = ("/challenges", "/scoreboard", "/login", "/register",
                      "/users", "/teams", "/rules")

# Bộ field đặc trưng của GZCTF ClientConfig (GET /api/config)
_GZCTF_CONFIG_FIELDS = ("Title", "Slogan", "PortMapping", "DefaultLifetime")

# Envelope JSON đặc trưng của rCTF: {"kind": "...", "message": ..., "data": ...}
_RCTF_KIND_RE = re.compile(r"^(good|bad|unauth)")

_PLATFORM_CLASSES = {
    "gzctf": GZCTFPlatform,
    "ctfd": CTFdPlatform,
    "rctf": RCTFPlatform,
    "custom_rest": CustomRESTPlatform,
    "generic_html": GenericHTMLPlatform,
}

_PLATFORM_LABELS = {
    "gzctf": "GZ::CTF",
    "ctfd": "CTFd",
    "rctf": "rCTF",
    "custom_rest": "Custom REST / Next.js CTF",
    "generic_html": "Generic HTML",
}


class PlatformDetector:
    # ------------------------------------------------------------------ #
    # Tiện ích
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize(base_url: str):
        """Chuẩn hoá URL: lược fragment/suffix phổ biến, trả (parsed, origin, clean_base_url)."""
        base_url = base_url.split("#")[0].rstrip("/")
        parsed = urllib.parse.urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        path = parsed.path.rstrip("/")
        for suffix in _URL_PATH_SUFFIXES:
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                break
        clean_base_url = f"{origin}{path}".rstrip("/") or origin
        return parsed, origin, clean_base_url

    @staticmethod
    def _get(session: requests.Session, url: str):
        """GET an toàn: trả response hoặc None (mọi exception bị nuốt)."""
        try:
            return session.get(url, timeout=5)
        except Exception:
            return None

    @staticmethod
    def _get_json(session: requests.Session, url: str, statuses=(200,)):
        """GET và parse JSON. Trả (data|None, status_code|None)."""
        resp = PlatformDetector._get(session, url)
        status = getattr(resp, "status_code", None)
        if resp is None or status not in statuses:
            return None, status
        try:
            return resp.json(), status
        except Exception:
            return None, status

    # ------------------------------------------------------------------ #
    # Tầng 3 — các probe API (mock-able qua session.get), cũng dùng để
    # làm giàu capabilities ngay cả khi nền tảng đã biết từ tầng 1.
    # `done` chặn probe lặp lại giữa lúc dò và lúc làm giàu.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _probe_gzctf(origin: str, session: requests.Session, info: PlatformInfo, done: set) -> bool:
        """/api/config (ClientConfig) hoặc /api/game/recent|/api/game (ArrayResponse)."""
        if "gzctf_config" not in done:
            done.add("gzctf_config")
            data, status = PlatformDetector._get_json(session, f"{origin}/api/config")
            if isinstance(data, dict) and all(f in data for f in _GZCTF_CONFIG_FIELDS):
                caps = info.capabilities
                info.version_hints["title"] = data.get("Title")
                caps["rules_via_api"] = bool(data.get("Rules"))
                public_key = data.get("ApiPublicKey")
                caps["api_encryption"] = bool(public_key)
                caps["port_mapping_proxy"] = data.get("PortMapping") == "PlatformProxy"
                info.add_signal(
                    f"/api/config khớp ClientConfig GZCTF "
                    f"(ApiPublicKey={'có' if public_key else 'null'}, "
                    f"PortMapping={data.get('PortMapping')!r})"
                )
                return True
            info.add_signal(f"GET /api/config -> không khớp GZCTF (HTTP {status})")

        if "gzctf_games" not in done:
            done.add("gzctf_games")
            for endpoint in ("/api/game/recent", "/api/game"):
                data, _status = PlatformDetector._get_json(session, f"{origin}{endpoint}")
                if isinstance(data, dict) and "data" in data and ("length" in data or "total" in data):
                    info.add_signal(f"GET {endpoint} -> ArrayResponse GZCTF {{data, length, total}}")
                    return True
            info.add_signal("GET /api/game/recent|/api/game -> không khớp GZCTF")
        return False

    @staticmethod
    def _probe_ctfd(origin: str, session: requests.Session, info: PlatformInfo, done: set) -> bool:
        """/api/v1/challenges -> envelope {"success": ...}; phát hiện plugin whale."""
        if "ctfd_challs" in done:
            return False
        done.add("ctfd_challs")
        data, status = PlatformDetector._get_json(session, f"{origin}/api/v1/challenges",
                                                  statuses=(200, 401, 403))
        if isinstance(data, dict) and "success" in data:
            try:
                dumped = json.dumps(data)
            except Exception:
                dumped = ""
            if "ctfd-whale" in dumped:
                info.capabilities["container"] = True
                info.version_hints["whale_fork"] = "frankli0324/ctfd-whale"
                info.add_signal("/api/v1/challenges có template/script /plugins/ctfd-whale/ "
                                "-> hỗ trợ container động (whale fork)")
            info.add_signal('GET /api/v1/challenges -> envelope {"success": ...} của CTFd')
            return True
        info.add_signal(f"GET /api/v1/challenges -> không khớp CTFd (HTTP {status})")
        return False

    @staticmethod
    def _probe_rctf(origin: str, session: requests.Session, info: PlatformInfo, done: set) -> bool:
        """/api/v1/challs -> envelope {kind, message, data}; badEndpoint cũng là dấu hiệu rCTF."""
        if "rctf_challs" in done:
            return False
        done.add("rctf_challs")
        data, status = PlatformDetector._get_json(session, f"{origin}/api/v1/challs",
                                                  statuses=(200, 401, 403))
        kind = data.get("kind") if isinstance(data, dict) else None
        if isinstance(kind, str) and _RCTF_KIND_RE.match(kind):
            info.capabilities["scoreboard"] = True
            info.add_signal(f"GET /api/v1/challs -> envelope rCTF kind={kind}")
            return True
        info.add_signal(f"GET /api/v1/challs -> không khớp rCTF (HTTP {status})")
        return False

    # ------------------------------------------------------------------ #
    # API mới: trả (platform_instance, PlatformInfo)
    # ------------------------------------------------------------------ #
    @staticmethod
    def detect_platform_info(base_url: str, session: requests.Session,
                             cookie_hint: Optional[str] = None) -> Tuple[BasePlatform, PlatformInfo]:
        """
        Dò tìm nền tảng CTF theo pipeline 4 tầng, trả về platform instance
        (tương thích hoàn toàn với chữ ký/cách dùng cũ) kèm PlatformInfo.
        """
        parsed, origin, clean_base_url = PlatformDetector._normalize(base_url)
        info = PlatformInfo(platform_type="unknown", base_url=clean_base_url)

        game_match = re.search(r"/games?/(\d+)", parsed.path)
        if game_match:
            info.game_id = int(game_match.group(1))
            info.add_signal(f"URL chứa /games/{info.game_id} -> game_id={info.game_id}")

        done: set = set()
        ptype, confidence = "unknown", "low"

        # ---------------- Tầng 1: HTML markers ---------------- #
        html = ""
        resp = PlatformDetector._get(session, clean_base_url)
        if resp is not None and getattr(resp, "status_code", 0) == 200:
            html = getattr(resp, "text", "") or ""

        if html:
            low = html.lower()
            if 'name="rctf-config"' in low or re.search(r'"kind"\s*:\s*"', html):
                ptype, confidence = "rctf", "high"
                info.add_signal("HTML marker: <meta name=\"rctf-config\"> hoặc envelope {kind,message,data}")
            elif ("csrfnonce'" in low or "window.init" in low
                  or "powered by ctfd" in low or "/themes/core/" in low):
                ptype, confidence = "ctfd", "high"
                info.add_signal("HTML marker: csrfNonce' / window.init / Powered by CTFd / themes/core")
            elif "gzctf" in low or "gz::ctf" in low:
                ptype, confidence = "gzctf", "high"
                info.add_signal("HTML marker: <meta keywords> GZCTF hoặc chuỗi GZCTF/GZ::CTF")
            else:
                info.add_signal("HTML gốc không chứa marker nhận diện nào")

        # ---------------- Tầng 2: Cookies ---------------- #
        if confidence != "high":
            try:
                cookie_names = set(session.cookies.keys())
            except Exception:
                cookie_names = set()

            if "GZCTF_Token" in cookie_names or (
                    cookie_hint and "gzctf_token" in cookie_hint.lower()):
                ptype, confidence = "gzctf", "medium"
                info.add_signal("Cookie GZCTF_Token trong cookie jar/hint -> nghi GZ::CTF")
            elif "_xsrf" in cookie_names:
                # RootTheBox dùng cookie _xsrf nhưng chưa có adapter riêng
                info.add_signal("Cookie _xsrf -> nghi RootTheBox (chưa có adapter)")
            elif "session" in cookie_names:
                ptype, confidence = "ctfd", "medium"
                info.add_signal("Cookie Flask 'session' vừa được set -> nghi CTFd")

        # ------------- Tầng 3: Path probe + envelope ------------- #
        # Chạy đủ chuỗi probe (theo thứ tự rẻ -> chắc chắn): vừa xác nhận ứng
        # viên ở tầng 2, vừa làm giàu capabilities khi tầng 1 đã nhận diện xong.
        for candidate, probe in (("gzctf", PlatformDetector._probe_gzctf),
                                 ("ctfd", PlatformDetector._probe_ctfd),
                                 ("rctf", PlatformDetector._probe_rctf)):
            if probe(origin, session, info, done):
                if confidence != "high":
                    ptype, confidence = candidate, "high"
                break

        # ------------- Tầng 4: Fallback hành vi cũ ------------- #
        if confidence != "high":
            # Hành vi cũ: Custom REST / Next.js (/api/challenges, /api/auth/me)
            data, status = PlatformDetector._get_json(session, f"{origin}/api/challenges",
                                                      statuses=(200, 401, 403))
            payload = data.get("data") if isinstance(data, dict) else None
            if isinstance(data, dict) and data.get("success") and isinstance(payload, dict) \
                    and "challenges" in payload:
                ptype, confidence = "custom_rest", "high"
                info.add_signal(f"GET /api/challenges -> shape Custom REST (HTTP {status})")
            else:
                data, status = PlatformDetector._get_json(session, f"{origin}/api/auth/me",
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
            Logger.warning("Không xác định được nền tảng. Fallback sang GenericHTMLPlatform.")
            info.add_signal("Fallback: mọi tầng nhận diện thất bại -> generic HTML scraper")
            ptype = "generic_html"
            info.capabilities["scoreboard"] = False  # scraper HTML không có scoreboard API

        platform = _PLATFORM_CLASSES[ptype](clean_base_url, session)
        info.platform_type = getattr(platform.ctf_info, "platform_type", ptype)
        platform_game_id = getattr(platform, "game_id", None)
        if isinstance(platform_game_id, int):
            info.game_id = platform_game_id
        info.confidence = confidence

        # setattr mềm: các class platform không cần khai báo sẵn thuộc tính info
        platform.info = info

        Logger.info(
            f"Detected Platform: [bold green]{_PLATFORM_LABELS.get(ptype, ptype)}[/bold green] "
            f"(confidence: [bold yellow]{confidence}[/bold yellow])"
        )
        return platform, info

    # ------------------------------------------------------------------ #
    # API cũ — giữ nguyên chữ ký & giá trị trả về (platform instance)
    # ------------------------------------------------------------------ #
    @staticmethod
    def detect_platform(base_url: str, session: requests.Session) -> BasePlatform:
        """
        Auto-detects the CTF platform (CTFd, rCTF, GZCTF, Custom REST, Generic).
        Wrapper tương thích quanh detect_platform_info(): instance trả về luôn
        mang thuộc tính `.info` (PlatformInfo) để caller mới tận dụng recon.
        """
        platform, _info = PlatformDetector.detect_platform_info(base_url, session)
        return platform
