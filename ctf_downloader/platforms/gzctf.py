import hashlib
import json
import re
import time
import urllib.parse
import requests
from typing import List, Dict, Any, Optional, Tuple
from rich.markup import escape
from .base import (BasePlatform, Challenge, CTFInfo, EventTimes,
                   PlatformRegisterUnsupported, SolveAttribution, epoch_ms,
                   normalize_epoch_to_utc, safe_get_json)
from ..utils.gzctf_crypto import GZCTFCryptoError, encrypt_api_data
from ..utils.logger import Logger
from .registry import register

# Semantic fields của GZCTF ClientConfig. Upstream hiện tại serialize camelCase,
# các bản cũ/test fixtures từng dùng PascalCase -> luôn đọc tương thích cả hai.
_GZCTF_CONFIG_FIELDS = ("title", "slogan", "portMapping", "defaultLifetime")


def _gz_get(data: Dict[str, Any], *names: str, default=None):
    if not isinstance(data, dict):
        return default
    by_lower = {str(k).lower(): v for k, v in data.items()}
    for name in names:
        if name in data:
            return data[name]
        hit = by_lower.get(str(name).lower())
        if hit is not None:
            return hit
    return default


def _gz_client_config(platform, max_age: float = 300.0) -> Dict[str, Any]:
    now = time.monotonic()
    cached = getattr(platform, "_register_client_config", None)
    cached_at = getattr(platform, "_register_client_config_ts", None)
    if (isinstance(cached, dict) and cached_at is not None
            and now - float(cached_at) < max_age):
        return cached
    data, status = safe_get_json(
        platform.session, f"{platform.origin}/api/config", statuses=(200,)
    )
    if not isinstance(data, dict):
        raise PlatformRegisterUnsupported(
            f"Không đọc được GZCTF /api/config (HTTP {status}) — "
            "không thể xác minh API encryption/captcha an toàn."
        )
    platform._register_client_config = data
    platform._register_client_config_ts = now
    return data


def probe_api_config(origin: str, session, info, done: set) -> bool:
    """/api/config (ClientConfig): xác nhận GZCTF + làm giàu capabilities."""
    if "gzctf_config" in done:
        return False
    done.add("gzctf_config")
    data, status = safe_get_json(session, f"{origin}/api/config")
    if isinstance(data, dict) and all(
        _gz_get(data, field) is not None for field in _GZCTF_CONFIG_FIELDS
    ):
        caps = info.capabilities
        info.version_hints["title"] = _gz_get(data, "title", "Title")
        caps["rules_via_api"] = bool(_gz_get(data, "rules", "Rules"))
        public_key = _gz_get(data, "apiPublicKey", "ApiPublicKey")
        port_mapping = _gz_get(data, "portMapping", "PortMapping")
        caps["api_encryption"] = bool(public_key)
        caps["port_mapping_proxy"] = port_mapping == "PlatformProxy"
        info.add_signal(
            f"/api/config khớp ClientConfig GZCTF "
            f"(apiPublicKey={'có' if public_key else 'null'}, "
            f"portMapping={port_mapping!r})"
        )
        return True
    info.add_signal(f"GET /api/config -> không khớp GZCTF (HTTP {status})")
    return False


def probe_game_recent(origin: str, session, info, done: set) -> bool:
    """/api/game/recent|/api/game (ArrayResponse {data, length, total})."""
    if "gzctf_games" in done:
        return False
    done.add("gzctf_games")
    for endpoint in ("/api/game/recent", "/api/game"):
        data, _status = safe_get_json(session, f"{origin}{endpoint}")
        if isinstance(data, dict) and "data" in data and ("length" in data or "total" in data):
            info.add_signal(f"GET {endpoint} -> ArrayResponse GZCTF {{data, length, total}}")
            return True
    info.add_signal("GET /api/game/recent|/api/game -> không khớp GZCTF")
    return False


# --------------------------------------------------------------------------- #
# Auto-register (spec auto-register §2): /api/config -> captcha gate ->
# POST /api/account/register -> login ngay lấy GZCTF_Token.
#
# Van-an-toàn (khối lượng xử lý ở RegisterService, tầng platform chỉ làm HTTP):
#  - Turnstile/reCAPTCHA/hCaptcha bật  -> DỪNG SẠCH, KHÔNG BAO GIỜ bypass.
#  - HashPow (PoW server-side)         -> giải được bằng CPU, tự động.
# --------------------------------------------------------------------------- #
_CAPTCHA_SITEKEY_FIELDS = ("TurnstileSiteKey", "RecaptchaSiteKey", "HCaptchaSiteKey")
_CAPTCHA_PROVIDER_VALUES = ("turnstile", "recaptchav2", "recaptchav3",
                            "recaptcha", "hcaptcha")


def solve_hash_pow(challenge_hex: str, difficulty: int,
                   max_iter: int = 50_000_000) -> Optional[str]:
    """Solve current GZCTF HashPow against the *challenge bytes*.

    Upstream returns two distinct values: ``id`` is only the cache/ticket key,
    while ``challenge`` is the random 8-byte preimage prefix that the browser
    worker hashes. Server verification computes SHA256(challenge + answer).
    The answer is always exactly 8 bytes / 16 hex chars, even difficulty=0.
    """
    try:
        prefix = bytes.fromhex(str(challenge_hex))
    except ValueError:
        prefix = str(challenge_hex).encode()
    if difficulty <= 0:
        return (0).to_bytes(8, "big").hex()
    for i in range(max_iter):
        answer_bytes = i.to_bytes(8, "big")
        digest = hashlib.sha256(prefix + answer_bytes).digest()
        bits = 0
        for byte in digest:
            if byte == 0:
                bits += 8
                continue
            bits += (8 - byte.bit_length())
            break
        if bits >= difficulty:
            return answer_bytes.hex()
    return None


def gzctf_probe_captcha(platform) -> Optional[Dict[str, Any]]:
    """Kiểm tra cấu hình captcha trước khi register.

    GZCTF hiện đại: KHÔNG bật captcha thì GET /api/captcha vẫn trả
    HTTP 200 ``{"type": "None", "siteKey": ""}`` — đó là KHÔNG captcha,
    phải đi tiếp (không được coi là "captcha lạ").

    Returns:
        dict PoW task {'challenge_id','difficulty'} nếu cần giải HashPow;
        {} nếu platform KHÔNG yêu cầu captcha nào; raise
        PlatformRegisterUnsupported nếu Turnstile/reCAPTCHA/hCaptcha bật.
    """
    origin, sess = platform.origin, platform.session

    # 1. ClientConfig: cache + support both old PascalCase/new camelCase.
    cfg = _gz_client_config(platform)
    # Historical deployments exposed provider/site keys here; current upstream
    # exposes provider via /api/captcha. Keep the old checks for compatibility.
    provider = str(_gz_get(cfg, "captchaProvider", "CaptchaProvider") or "").strip().lower()
    site_key_on = any(str(_gz_get(cfg, k, k[0].lower() + k[1:]) or "").strip()
                      for k in _CAPTCHA_SITEKEY_FIELDS)
    if provider and provider not in ("none", "hashpow", "pow"):
        raise PlatformRegisterUnsupported(
            "⚠️ Platform bật captcha "
            f"({provider}) — đăng ký thủ công tại {origin}/register. "
            "Tool không bypass captcha.")
    if site_key_on:
        raise PlatformRegisterUnsupported(
            "⚠️ Platform cấu hình site-key captcha (Turnstile/reCAPTCHA/"
            "hCaptcha) — đăng ký thủ công. Tool không bypass captcha.")

    # 2. GET /api/captcha: 404/204/rỗng (GZCTF cũ) HOẶC 200 {"type":"None",
    #    "siteKey":""} (GZCTF hiện đại) -> không cần captcha, đi tiếp.
    try:
        resp = sess.get(f"{origin}/api/captcha", timeout=15)
    except Exception as exc:
        # Fail closed. Treating a network failure as "captcha disabled" can
        # submit a registration without the required token and burn the
        # platform's register rate limit.
        raise PlatformRegisterUnsupported(
            f"Không kiểm tra được captcha GZCTF ({type(exc).__name__}: {exc}) — "
            "dừng trước khi POST register."
        ) from exc
    if resp.status_code in (404, 204):
        # Compatibility with older GZCTF versions where the captcha endpoint
        # did not exist when disabled.
        return {}
    if resp.status_code != 200:
        raise PlatformRegisterUnsupported(
            f"GET /api/captcha trả HTTP {resp.status_code} — "
            "không thể xác minh captcha an toàn."
        )
    try:
        data = resp.json()
    except Exception as exc:
        raise PlatformRegisterUnsupported(
            "GET /api/captcha trả dữ liệu không phải JSON — dừng an toàn."
        ) from exc
    if not isinstance(data, dict):
        raise PlatformRegisterUnsupported(
            "GET /api/captcha trả shape không hợp lệ — dừng an toàn."
        )
    if not data:
        return {}

    ctype = str(data.get("type") or data.get("captchaType") or "").strip()
    ctype_low = ctype.lower()
    site_key = str(data.get("siteKey") or "").strip()

    # type "None" + siteKey rỗng = platform TẮT captcha (HTTP 200) -> đi tiếp.
    if ctype_low in ("none", "") and not site_key:
        return {}

    # Turnstile/reCAPTCHA/hCaptcha (theo type hoặc siteKey có giá trị) -> dừng.
    if any(marker in ctype_low for marker in
           ("turnstile", "recaptcha", "hcaptcha")) or site_key:
        raise PlatformRegisterUnsupported(
            f"⚠️ Platform bật captcha ({ctype or 'siteKey'}) — đăng ký thủ "
            f"công tại {origin}/register. Tool không bypass captcha.")

    # HashPow -> lấy challenge từ /api/captcha/PowChallenge và giải.
    if "pow" in ctype_low or "hashpow" in ctype_low:
        # ASP.NET routing is case-insensitive; keep historical casing so
        # older reverse proxies/fixtures do not let the generic /api/captcha
        # handler swallow this more-specific request.
        chal_url = f"{origin}/api/captcha/PowChallenge"
        chal_resp = sess.get(chal_url, timeout=15)
        if chal_resp.status_code != 200:
            raise PlatformRegisterUnsupported(
                f"Lấy PowChallenge thất bại (HTTP {chal_resp.status_code}).")
        chal = chal_resp.json() or {}
        chal_id = str(_gz_get(chal, "id", "Id") or "")
        challenge_raw = _gz_get(chal, "challenge", "Challenge")
        if not chal_id:
            raise PlatformRegisterUnsupported(
                f"PowChallenge thiếu id: {chal}")
        # Current upstream always sends a distinct random challenge. Very old
        # builds/fixtures exposed only id; retain a legacy fallback without
        # affecting modern servers where challenge is present.
        challenge = str(challenge_raw or chal_id)
        if challenge_raw is None:
            Logger.warning(
                "GZCTF PowChallenge legacy thiếu field challenge — fallback hash id."
            )
        raw_diff = _gz_get(chal, "difficulty", "Difficulty")
        try:
            difficulty = int(raw_diff or 0)
        except (TypeError, ValueError):
            try:
                difficulty = int(float(raw_diff))
            except (TypeError, ValueError):
                # difficulty dị ("easy", object...) -> từ chối sạch, không rò
                # rẽ exception lạ ra ngoài hợp đồng register (C10-04).
                raise PlatformRegisterUnsupported(
                    f"⚠️ PowChallenge difficulty không hợp lệ ({raw_diff!r}) — "
                    f"đăng ký thủ công tại {origin}/register.")
        return {
            "challenge_id": chal_id,
            "challenge": challenge,
            "difficulty": difficulty,
        }

    # Loại captcha khác/không rõ -> an toàn là dừng
    raise PlatformRegisterUnsupported(
        f"⚠️ Platform yêu cầu captcha không nhận diện được ({data}) — "
        "đăng ký thủ công. Tool không bypass captcha.")


def _gz_captcha_ticket(platform) -> Optional[str]:
    """Get a fresh single-use captcha ticket for exactly one protected action."""
    task = gzctf_probe_captcha(platform)
    if not task:
        return None
    solution = solve_hash_pow(task["challenge"], task["difficulty"])
    if solution is None:
        raise PlatformRegisterUnsupported(
            "Không giải được GZCTF HashPow trong giới hạn vòng lặp."
        )
    if len(solution) != 16:
        raise PlatformRegisterUnsupported(
            f"GZCTF HashPow solver trả answer sai độ dài: {solution!r}"
        )
    return f"{task['challenge_id']}:{solution}"


def _gz_capture_auth(session) -> Dict[str, Any]:
    cookies = {c.name: c.value for c in getattr(session, "cookies", [])}
    token = cookies.get("GZCTF_Token")
    out: Dict[str, Any] = {}
    if cookies:
        out["cookies"] = cookies
    if token:
        out["token"] = token
    return out


def gzctf_register(platform, *, username: str, email: str, password: str,
                   verify_email_hook=None) -> Dict[str, Any]:
    """Auto-register against current GZCTF while retaining old-server fallback.

    Current upstream requires API encryption when ``apiPublicKey`` is present,
    a fresh single-use captcha ticket for every protected action, and returns a
    RegisterStatus that tells us whether login/email/admin confirmation is next.
    """
    origin, sess = platform.origin, platform.session

    cfg = _gz_client_config(platform)
    public_key = _gz_get(cfg, "apiPublicKey", "ApiPublicKey")
    try:
        wire_password = encrypt_api_data(password, public_key)
    except GZCTFCryptoError as exc:
        raise PlatformRegisterUnsupported(
            f"Không mã hoá được password theo GZCTF apiPublicKey: {exc}"
        ) from exc

    payload: Dict[str, Any] = {
        "userName": username,
        "email": email,
        "password": wire_password,
    }
    ticket = _gz_captcha_ticket(platform)
    if ticket:
        payload["challenge"] = ticket
        Logger.info("GZCTF: đã giải HashPow cho register.")

    try:
        resp = sess.post(f"{origin}/api/account/register", json=payload,
                         timeout=20)
    except Exception as exc:
        return {"ok": False, "message": f"Lỗi mạng khi register: {exc}"}

    if resp.status_code != 200:
        detail = (resp.text or "").strip().strip('"')[:300]
        return {"ok": False,
                "message": f"Register thất bại (HTTP {resp.status_code}): {detail}"}

    body: Dict[str, Any] = {}
    try:
        parsed = resp.json() or {}
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        pass
    register_status = str(_gz_get(body, "data", "Data") or "").strip()
    title = str(_gz_get(body, "title", "Title") or "Đã register")
    result: Dict[str, Any] = {
        "ok": True,
        "message": title,
        "register_status": register_status or None,
    }

    if register_status == "LoggedIn":
        Logger.success("GZCTF: register OK và server đã đăng nhập session.")
        result.update(_gz_capture_auth(sess))
        return result

    if register_status == "AdminConfirmationRequired":
        result["pending_admin_confirmation"] = True
        Logger.warning(
            "GZCTF: account đã tạo nhưng đang chờ admin phê duyệt; "
            "không thử login lặp lại."
        )
        return result

    if register_status == "EmailConfirmationRequired":
        result["pending_email_verification"] = True
        if verify_email_hook is None:
            Logger.warning(
                "GZCTF: account đang chờ email verification; hãy mở email thủ công."
            )
            return result
        verified = verify_email_hook(
            sess, platform="gzctf", base_url=origin
        )
        verified_ok = bool(
            verified.get("ok") if isinstance(verified, dict) else verified
        )
        result["email_verified"] = verified_ok
        result["pending_email_verification"] = not verified_ok
        if verified_ok:
            # AccountController.Verify signs the user in server-side.
            result.update(_gz_capture_auth(sess))
        return result

    # Compatibility fallback for older GZCTF builds that returned HTTP 200
    # without RegisterStatus. They may need a separate login request. LoginModel
    # is also ModelWithCaptcha, so fetch a FRESH ticket and use current key name.
    Logger.info(
        "GZCTF register trả status legacy/không rõ — thử login tương thích."
    )
    login_payload: Dict[str, Any] = {
        "userName": username,
        "password": wire_password,
    }
    login_ticket = _gz_captcha_ticket(platform)
    if login_ticket:
        login_payload["challenge"] = login_ticket
        Logger.info("GZCTF: đã lấy HashPow mới riêng cho login.")
    try:
        login = sess.post(
            f"{origin}/api/account/login", json=login_payload, timeout=20
        )
        if login.status_code == 200:
            result.update(_gz_capture_auth(sess))
            # Some old deployments returned a raw token body instead of cookie.
            raw_token = (login.text or "").strip().strip('"')
            if raw_token and raw_token not in ("{}", "null") and not result.get("token"):
                result["token"] = raw_token
        else:
            Logger.warning(
                f"GZCTF: login sau register thất bại (HTTP {login.status_code}) "
                "— dùng credentials vừa in để đăng nhập thủ công."
            )
    except PlatformRegisterUnsupported:
        raise
    except Exception as exc:
        Logger.warning(f"GZCTF: login sau register lỗi: {exc}")
    return result


@register("gzctf", label="GZ::CTF", throttle=2.0,
          html_markers=("GZCTF", "GZ::CTF"),
          cookie_hints=("GZCTF_Token",),
          probes=(probe_api_config, probe_game_recent),
          supports_container=True, supports_scoreboard=True, rules_via_api=True)
class GZCTFPlatform(BasePlatform):
    # Số lần poll tối đa kết quả chấm của một submission
    SUBMISSION_POLL_ATTEMPTS = 6
    SUBMISSION_POLL_INTERVAL = 1.0  # giây

    # TTL cache solve-attribution (giây) — cùng pattern CTFd/rCTF: watch tạo
    # platform 1 lần/process, không TTL thì by_team/by_other đóng băng.
    SOLVE_ATTR_TTL: float = 300.0

    def __init__(self, base_url: str, session: requests.Session):
        # Extract game_id from base_url if present (e.g., https://.../games/6/challenges)
        parsed = urllib.parse.urlparse(base_url)
        self.origin = f"{parsed.scheme}://{parsed.netloc}"

        self.game_id: Optional[int] = None

        # Ưu tiên 1: query ?gid=<id>
        try:
            qs = urllib.parse.parse_qs(parsed.query or "")
            if "gid" in qs and qs["gid"]:
                self.game_id = int(qs["gid"][0])
        except (ValueError, TypeError):
            self.game_id = None

        # Ưu tiên 2: path /games/<id> hoặc /game/<id>
        if self.game_id is None:
            game_match = re.search(r'/games?/(\d+)', parsed.path)
            if game_match:
                self.game_id = int(game_match.group(1))

        # KHÔNG brute-force probe id nữa — nếu không suy ra được từ URL thì để None.

        super().__init__(self.origin, session)
        self.ctf_info.platform_type = "gzctf"

    def authenticate(self) -> bool:
        """
        Validates authentication on GZCTF via /api/account/profile and /api/game/{id}.
        Không dò (brute-force) game id — game_id phải suy ra được từ URL người dùng.
        """
        # 1. Check Profile
        profile_ok = False
        try:
            resp = self.session.get(f"{self.origin}/api/account/profile", timeout=15)
            if resp.status_code == 200:
                user_data = resp.json()
                self.ctf_info.user_name = user_data.get("userName") or user_data.get("realName")
                Logger.success(f"Đã xác thực GZCTF với User: [info]{escape(str(self.ctf_info.user_name))}[/info] ({escape(str(user_data.get('email')))})", markup=True)
                profile_ok = True
        except Exception:
            pass

        # 2. Check Game Info (chỉ khi biết chắc game_id từ URL)
        if self.game_id is not None:
            try:
                resp = self.session.get(f"{self.origin}/api/game/{self.game_id}", timeout=15)
                if resp.status_code == 200:
                    game_data = resp.json()
                    self.ctf_info.title = game_data.get("title", f"Game {self.game_id}")
                    self.ctf_info.team_name = game_data.get("teamName")
                    if self.ctf_info.team_name:
                        Logger.info(f"[fg.faint]Team:[/fg.faint] [fg.base]{escape(str(self.ctf_info.team_name))}[/fg.base] | [fg.faint]Competition:[/fg.faint] [fg.base]{escape(str(self.ctf_info.title))}[/fg.base]", markup=True)
                    return True
            except Exception as e:
                Logger.warning(f"Không lấy được thông tin game {self.game_id}: {e}")

        if profile_ok:
            Logger.warning("Không xác định được game_id từ URL (vd: https://host/games/<id>/challenges). Một số tính năng sẽ bị giới hạn.")
            return True

        Logger.error("Xác thực thất bại trên nền tảng GZCTF. Hãy kiểm tra lại cookie GZCTF_Token.")
        return False

    def register(self, *, username: str, email: str, password: str,
                 verify_email_hook=None) -> Dict[str, Any]:
        """Auto-register GZCTF — xem gzctf_register (spec auto-register §2)."""
        return gzctf_register(self, username=username, email=email,
                              password=password,
                              verify_email_hook=verify_email_hook)

    def fetch_rules(self) -> Optional[str]:
        """
        Lấy rules / mô tả định dạng flag từ /api/game/{game_id} (field 'content', public).
        Trả về None nếu không biết game_id hoặc request lỗi.
        """
        if not self.game_id:
            return None
        try:
            resp = self.session.get(f"{self.origin}/api/game/{self.game_id}", timeout=15)
            if resp.status_code == 200:
                content = (resp.json() or {}).get("content")
                if content and str(content).strip():
                    return str(content)
        except Exception as e:
            Logger.warning(f"Không lấy được rules từ game {self.game_id}: {e}")
        return None

    def fetch_challenges(self) -> List[Challenge]:
        """
        Fetches all challenges and detailed metadata from GZCTF.
        """
        if not self.game_id:
            Logger.error("GZCTF: không xác định được game_id từ URL — bỏ qua fetch challenges.")
            return []
        details_url = f"{self.origin}/api/game/{self.game_id}/details"
        try:
            resp = self.session.get(details_url, timeout=20)
            if resp.status_code != 200:
                Logger.error(f"Không tải được challenges từ {details_url} (HTTP {resp.status_code})")
                return []

            data = resp.json()
            raw_categories = data.get("challenges", {})
            if not raw_categories:
                Logger.warning("Chi tiết game không trả về challenge nào.")
                return []

            total_count = sum(len(challs) for challs in raw_categories.values())
            Logger.info(f"Tìm thấy {total_count} challenges trong {len(raw_categories)} categories trên GZCTF. Đang tải chi tiết...")

            # Fetch solved challenge IDs from scoreboard for current user/team
            solved_chall_ids = set()
            try:
                sb_resp = self.session.get(f"{self.origin}/api/game/{self.game_id}/scoreboard", timeout=10)
                if sb_resp.status_code == 200:
                    sb_json = sb_resp.json()
                    sb_items = sb_json.get("items", []) if isinstance(sb_json, dict) else sb_json
                    for s_item in sb_items:
                        is_my = False
                        if self.ctf_info.team_name and s_item.get("name") == self.ctf_info.team_name:
                            is_my = True
                        elif self.ctf_info.user_name and s_item.get("name") == self.ctf_info.user_name:
                            is_my = True
                        elif self.ctf_info.user_name:
                            for sol in s_item.get("solvedChallenges", []):
                                if sol.get("userName") == self.ctf_info.user_name:
                                    is_my = True
                                    break
                        if is_my:
                            self.ctf_info.team_name = s_item.get("name")
                            for sol in s_item.get("solvedChallenges", []):
                                if sol.get("id"):
                                    solved_chall_ids.add(sol.get("id"))
                            break
            except Exception:
                pass

            detailed_challenges = []

            for category_name, chall_list in raw_categories.items():
                for item in chall_list:
                    chall_id = item.get("id")
                    title = (item.get("title") or f"Challenge_{chall_id}").strip()
                    score = item.get("score", 0)
                    solved_count = item.get("solved", 0)
                    is_solved = chall_id in solved_chall_ids

                    # Fetch individual challenge details: /api/game/{game_id}/challenges/{challenge_id}
                    single_url = f"{self.origin}/api/game/{self.game_id}/challenges/{chall_id}"
                    chall_resp = self.session.get(single_url, timeout=15)
                    
                    description = ""
                    hints_list = []
                    files_list = []
                    chall_type = item.get("type", "Standard")
                    single_data: Dict[str, Any] = {}

                    if chall_resp.status_code == 200:
                        try:
                            single_data = chall_resp.json() or {}
                            description = single_data.get("content") or ""
                            chall_type = single_data.get("type") or chall_type
                            
                            # Hints
                            for h in (single_data.get("hints") or []):
                                if isinstance(h, str):
                                    hints_list.append({"content": h})
                                elif isinstance(h, dict):
                                    hints_list.append(h)

                            # Attachments in context
                            ctx = single_data.get("context") or {}
                            if isinstance(ctx, dict):
                                asset_url = ctx.get("url")
                                if asset_url:
                                    full_asset_url = self.get_full_file_url(asset_url)
                                    filename = asset_url.rstrip("/").split("/")[-1]
                                    files_list.append((full_asset_url, filename))

                        except Exception as e:
                            Logger.warning(f"Lỗi parse chi tiết cho {title}: {e}")

                    # Determine if it's a dynamic container
                    is_container = chall_type == "DynamicContainer" or (single_data.get("type") == "DynamicContainer")
                    instance_info = {}
                    if is_container:
                        ctx = single_data.get("context") or {}
                        instance_info = {
                            "is_container": True,
                            "type": "gzctf",
                            "start_url": f"{self.origin}/api/game/{self.game_id}/container/{chall_id}",
                            "stop_url": f"{self.origin}/api/game/{self.game_id}/container/{chall_id}",
                            "extend_url": f"{self.origin}/api/game/{self.game_id}/container/{chall_id}/extend",
                            "status_url": f"{self.origin}/api/game/{self.game_id}/challenges/{chall_id}",
                            "entry": ctx.get("instanceEntry"),
                            "close_time": ctx.get("closeTime")
                        }

                    submit_endpoint = f"{self.origin}/api/game/{self.game_id}/challenges/{chall_id}"

                    chall_obj = Challenge(
                        id=chall_id,
                        name=title,
                        category=category_name,
                        points=score,
                        description=description,
                        tags=[chall_type] if chall_type else [],
                        hints=hints_list,
                        files=files_list,
                        solved_by_me=is_solved,
                        solves_count=solved_count,
                        submit_endpoint=submit_endpoint,
                        instance_info=instance_info,
                        raw_data=single_data or item
                    )
                    detailed_challenges.append(chall_obj)

            self.ctf_info.challenges = detailed_challenges
            self.ctf_info.game_id = self.game_id
            return detailed_challenges

        except Exception as e:
            Logger.error(f"Lỗi khi tải challenges GZCTF: {str(e)}")
            return []

    def get_full_file_url(self, file_path: str) -> str:
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(self.origin, file_path)

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """
        Submits a flag to GZCTF platform (/api/game/{game_id}/challenges/{challenge_id}).

        POST trả về submissionId -> poll GET .../Status/{submissionId}
        đến khi ra 'Accepted' (đúng) hoặc 'WrongAnswer' (sai).
        Cập nhật self.last_verdict: correct | incorrect | unknown | ratelimited.
        """
        self.last_verdict = "unknown"

        if not self.game_id:
            return False, "GZCTF: không xác định được game_id từ URL."

        url = f"{self.origin}/api/game/{self.game_id}/challenges/{challenge_id}"
        # Current GZCTF encrypts flag submissions with the same apiPublicKey
        # used for account secrets. Sending plaintext when ApiEncryption is on
        # makes DecryptApiData return null and can consume an attempt. Fail
        # closed if config/key is unreadable instead of guessing plaintext.
        try:
            cfg = _gz_client_config(self)
            public_key = _gz_get(cfg, "apiPublicKey", "ApiPublicKey")
            wire_flag = encrypt_api_data(flag.strip(), public_key)
        except (PlatformRegisterUnsupported, GZCTFCryptoError) as exc:
            self.last_verdict = "unknown"
            return False, f"Không chuẩn bị được GZCTF flag payload an toàn: {exc}"
        payload = {"flag": wire_flag}

        try:
            resp = self.session.post(url, json=payload, timeout=15)

            if resp.status_code == 200:
                sub_id = str(resp.text or "").strip().strip('"').strip()
                status_url = f"{url}/Status/{sub_id}"

                # Poll kết quả chấm thay vì đoán qua số lượng bloods
                for _attempt in range(self.SUBMISSION_POLL_ATTEMPTS):
                    raw_status = ""
                    try:
                        st_resp = self.session.get(status_url, timeout=10)
                        raw_status = str(st_resp.text or "").strip().strip('"').strip()
                    except Exception:
                        raw_status = ""
                    status_low = raw_status.lower()

                    if "accepted" in status_low:
                        self.last_verdict = "correct"
                        return True, f"🎉 FLAG CHÍNH XÁC! Đã giải xong challenge (Submission ID: {sub_id})!"
                    if "wronganswer" in status_low or "wrong_answer" in status_low or "wrong answer" in status_low:
                        self.last_verdict = "incorrect"
                        return False, f"❌ Flag không đúng (Submission ID: {sub_id})."
                    if "cheatdetected" in status_low or "cheat_detected" in status_low:
                        self.last_verdict = "cheat_detected"
                        return False, f"⚠️ GZCTF phát hiện flag không thuộc team hiện tại (Submission ID: {sub_id})."

                    time.sleep(self.SUBMISSION_POLL_INTERVAL)

                self.last_verdict = "unknown"
                return False, (
                    f"⚠️ Không xác định được kết quả chấm (Submission ID: {sub_id}). "
                    f"Hãy kiểm tra trang submissions của giải."
                )

            elif resp.status_code == 400:
                err_text = resp.text.strip().strip('"') or "Invalid Flag"
                if "cheat" in err_text.lower():
                    self.last_verdict = "cheat_detected"
                    return False, f"⚠️ GZCTF phát hiện flag không thuộc team ({err_text})."
                self.last_verdict = "incorrect"
                return False, f"❌ Flag không đúng ({err_text})."
            elif resp.status_code == 401:
                self.last_verdict = "auth_failed"
                return False, "🚫 Phiên xác thực hết hạn hoặc không hợp lệ."
            elif resp.status_code == 403:
                err_text = (resp.text or "").strip().lower()
                if "not started" in err_text or "notstarted" in err_text:
                    self.last_verdict = "event_not_started"
                elif "ended" in err_text or "closed" in err_text:
                    self.last_verdict = "event_closed"
                else:
                    self.last_verdict = "unknown"
                return False, "🚫 Bị từ chối truy cập / giải chưa hoạt động."
            elif resp.status_code == 429:
                self.last_verdict = "ratelimited"
                return False, "⏳ Rate limited. Vui lòng chờ rồi submit lại."
            else:
                self.last_verdict = "unknown"
                return False, f"Máy chủ trả HTTP {resp.status_code}: {resp.text[:100]}"

        except Exception as e:
            self.last_verdict = "unknown"
            return False, f"Ngoại lệ khi submit flag: {str(e)}"

    def start_instance(self, challenge_id: Any) -> Tuple[bool, Dict[str, Any]]:
        """
        Starts or retrieves container instance for challenge on GZCTF.
        """
        if not self.game_id:
            return False, {"message": "GZCTF: chưa xác định game_id."}
        url = f"{self.origin}/api/game/{self.game_id}/container/{challenge_id}"
        try:
            resp = self.session.post(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json() or {}
                entry = data.get("entry")
                # If entry is None, fetch from challenges details
                if not entry:
                    status_info = self.get_instance_status(challenge_id)
                    entry = status_info.get("entry")
                    data["entry"] = entry
                return True, data
            else:
                # Check if it is already running
                status_info = self.get_instance_status(challenge_id)
                if status_info.get("entry"):
                    return True, status_info
                return False, {"message": f"HTTP {resp.status_code}: {resp.text}"}
        except Exception as e:
            return False, {"message": str(e)}

    def stop_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """
        Stops/destroys container instance for challenge on GZCTF.
        """
        if not self.game_id:
            return False, "GZCTF: chưa xác định game_id."
        url = f"{self.origin}/api/game/{self.game_id}/container/{challenge_id}"
        try:
            resp = self.session.delete(url, timeout=15)
            if resp.status_code in [200, 204]:
                return True, "Đã dừng container."
            return False, f"Dừng container thất bại (HTTP {resp.status_code}): {resp.text}"
        except Exception as e:
            return False, str(e)

    def extend_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """
        Extends active container instance lifetime.
        """
        if not self.game_id:
            return False, "GZCTF: chưa xác định game_id."
        url = f"{self.origin}/api/game/{self.game_id}/container/{challenge_id}/extend"
        try:
            resp = self.session.post(url, timeout=15)
            if resp.status_code == 200:
                return True, "Đã gia hạn thời gian sống của container."
            return False, f"Gia hạn container thất bại (HTTP {resp.status_code}): {resp.text}"
        except Exception as e:
            return False, str(e)

    def get_instance_status(self, challenge_id: Any) -> Dict[str, Any]:
        """
        Fetches current container instance status and entry info.
        """
        if not self.game_id:
            return {"status": "unknown", "entry": None, "close_time": None,
                    "reason": "missing_game_id"}
        url = f"{self.origin}/api/game/{self.game_id}/challenges/{challenge_id}"
        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code == 200:
                data = resp.json() or {}
                ctx = data.get("context") or {}
                entry = ctx.get("instanceEntry")
                close_time = ctx.get("closeTime")
                return {
                    "status": "running" if entry else "stopped",
                    "entry": entry,
                    "close_time": close_time,
                    "type": data.get("type")
                }
            return {
                "status": "unknown",
                "entry": None,
                "close_time": None,
                "http_status": resp.status_code,
                "reason": "auth_failed" if resp.status_code in (401, 403)
                          else "http_error",
            }
        except Exception as exc:
            return {"status": "unknown", "entry": None, "close_time": None,
                    "reason": f"transport:{type(exc).__name__}"}

    # ------------------------------------------------------------------
    # Solve attribution (spec §4) — scoreboard là nguồn chính
    # ------------------------------------------------------------------

    def _team_member_names(self, team_id: Any) -> Optional[list]:
        """GET /api/team/{id}.members[].userName; retry 1 lần, None nếu vẫn lỗi."""
        for attempt in (1, 2):
            try:
                resp = self.session.get(f"{self.origin}/api/team/{team_id}", timeout=10)
                if resp.status_code == 200:
                    data = resp.json() or {}
                    members = data.get("members") or []
                    names = [m.get("userName") for m in members if isinstance(m, dict)]
                    return [n for n in names if n]
            except Exception:
                pass
        Logger.warning(
            f"GZCTF attribution: không xác minh được membership của team {team_id} "
            f"(sau 1 lần retry).")
        return None

    def _attribution_from_details(self, cache: dict) -> bool:
        """Fallback khi scoreboard 400/anonymized (trước giờ mở): /details chỉ
        báo own-team solve qua field ``solvedByMe``/``isSolved`` nếu có.
        Trả về False CHỈ khi request văng exception (lỗi mạng — payload từ
        chối rõ ràng như 4xx/5xx vẫn là True vì server trả lời được)."""
        try:
            resp = self.session.get(
                f"{self.origin}/api/game/{self.game_id}/details", timeout=15)
            if resp.status_code != 200:
                return True
            raw = ((resp.json() or {}).get("challenges")) or {}
            for challs in raw.values():
                for item in challs or []:
                    cid = item.get("id")
                    if cid is None:
                        continue
                    solved = bool(item.get("solvedByMe") or item.get("isSolved"))
                    if not solved:
                        continue
                    cache[str(cid)] = SolveAttribution(by_me=False, by_team=True)
            return True
        except Exception:
            return False

    def _fetch_all_attribution(self, cache: dict) -> bool:
        """Populate ``cache`` từ scoreboard (+details fallback). Trả về
        ``net_clean``: False CHỈ khi có request văng exception mạng — caller
        (R-L1) chỉ swap cache khi không mất dữ liệu oan do mạng đứt."""
        if not self.game_id:
            return True
        items = []
        net_clean = True
        try:
            sb = self.session.get(
                f"{self.origin}/api/game/{self.game_id}/scoreboard", timeout=10)
            if sb.status_code == 200:
                data = sb.json()
                items = data.get("items", []) if isinstance(data, dict) else data
        except Exception:
            items = []
            net_clean = False

        profile = self.ctf_info.user_name
        my_item = None
        confirmed_by_user = False
        for item in items or []:
            if not isinstance(item, dict):
                continue
            sols = item.get("solvedChallenges")
            if not isinstance(sols, list):
                sols = []
            if profile and any(isinstance(s, dict) and s.get("userName") == profile for s in sols):
                my_item = item
                confirmed_by_user = True
                break
            if self.ctf_info.team_name and item.get("name") == self.ctf_info.team_name:
                my_item = item
                break

        if my_item is not None and not confirmed_by_user:
            # Match theo tên đội — phải chốt membership bằng team members.
            # Fail-safe: KHÔNG xác minh được (request lỗi cả 2 lần) -> bỏ đội
            # này thay vì chấp nhận mạo danh (tránh kẹt solved_by_team/by_me sai).
            member_names = self._team_member_names(my_item.get("id"))
            if profile:
                if member_names is not None and profile in member_names:
                    confirmed_by_user = True
                else:
                    if member_names is not None:
                        Logger.warning(
                            "GZCTF attribution: team trùng tên nhưng profile không "
                            f"nằm trong members — bỏ qua '{my_item.get('name')}'.")
                    my_item = None

        if my_item is None:
            net_clean = self._attribution_from_details(cache) and net_clean
            return net_clean

        my_sols = my_item.get("solvedChallenges")
        if not isinstance(my_sols, list):
            my_sols = []
        for sol in my_sols:
            if not isinstance(sol, dict):
                continue
            cid = sol.get("id")
            if cid is None:
                continue
            uname = sol.get("userName")
            cache[str(cid)] = SolveAttribution(
                by_team=True,
                by_me=bool(profile) and uname == profile,
                solver_names=[uname] if uname else [],
                first_blood=bool(sol.get("firstBlood")),
                solved_at=epoch_ms(sol.get("time") or sol.get("date")),
            )
        return net_clean

    def fetch_solve_attribution(self, challenge_ids) -> Dict[Any, SolveAttribution]:
        """1–2 requests: /scoreboard (+ /team/{id} xác nhận membership).
        Cache kết quả trong phiên; mọi exception → trả phần đã có ({}).
        Cache có TTL (SOLVE_ATTR_TTL): hết hạn → fetch lại cho phiên watch
        dài."""
        wanted = {str(c): c for c in (challenge_ids or [])}
        now = time.monotonic()
        ts = getattr(self, "_solve_attr_ts", None)
        cache = getattr(self, "_solve_attr_cache", None)
        if cache is None or ts is None or (now - ts) >= self.SOLVE_ATTR_TTL:
            # R-L1: fetch vào dict local — chỉ SWAP cache + stamp ts khi fetch
            # KHÔNG văng exception mạng (payload dị/degraded vẫn tính thành
            # công nếu có dữ liệu hoặc server trả lời được). Fail giữ nguyên
            # data cũ + ts cũ → tick sau retry ngay thay vì chờ đủ TTL.
            fresh: Dict[str, SolveAttribution] = {}
            try:
                net_clean = self._fetch_all_attribution(fresh)
            except Exception:
                net_clean = False   # hợp đồng base.py: KHÔNG BAO GIỜ raise
            if net_clean or fresh:
                cache = self._solve_attr_cache = fresh
                self._solve_attr_ts = now
            elif cache is None:
                cache = {}   # chưa từng fetch thành công: trả rỗng
        return {orig: cache[k] for k, orig in wanted.items() if k in cache}

    def fetch_scoreboard(self, if_none_match: Optional[str] = None) -> Dict[str, Any]:
        """
        Fetches scoreboard and ranking standings from GZCTF.
        """
        result = {
            "title": self.ctf_info.title or "GZCTF Scoreboard",
            "my_team": self.ctf_info.team_name,
            "my_user": self.ctf_info.user_name,
            "my_rank": None,
            "my_score": None,
            "total_teams": 0,
            "standings": [],
            "_http_status": None,
            "_etag": None,
            "_retry_after": None,
            "_not_modified": False,
        }

        if not self.game_id:
            return result

        url = f"{self.origin}/api/game/{self.game_id}/scoreboard"
        try:
            req_headers = {"If-None-Match": if_none_match} if if_none_match else {}
            resp = self.session.get(url, timeout=15, headers=req_headers)
            result["_http_status"] = resp.status_code
            resp_headers = getattr(resp, "headers", None) or {}
            result["_etag"] = resp_headers.get("ETag") or if_none_match
            result["_retry_after"] = resp_headers.get("Retry-After")
            if resp.status_code == 304:
                result["_not_modified"] = True
                return result
            if resp.status_code == 200:
                data = resp.json() or {}
                items = data.get("items", []) if isinstance(data, dict) else data
                result["total_teams"] = len(items)
                standings = []
                for idx, entry in enumerate(items, 1):
                    name = entry.get("name") or entry.get("teamName") or entry.get("userName")
                    score = entry.get("score") or entry.get("totalScore") or 0
                    rank = entry.get("rank") or idx
                    
                    # Check if my_user is part of this team
                    is_my_team = False
                    if result["my_team"] and name == result["my_team"]:
                        is_my_team = True
                    elif result["my_user"] and name == result["my_user"]:
                        is_my_team = True
                    elif result["my_user"]:
                        for solved in entry.get("solvedChallenges", []):
                            if solved.get("userName") == result["my_user"]:
                                is_my_team = True
                                break

                    if is_my_team:
                        result["my_team"] = name
                        result["my_rank"] = f"{rank}th" if rank else f"{idx}th"
                        result["my_score"] = score

                    standings.append({
                        "pos": rank,
                        "name": name,
                        "score": score,
                        "raw": entry
                    })
                result["standings"] = standings
        except Exception as e:
            result["_error"] = f"{type(e).__name__}: {e}"
            Logger.warning(f"Không tải được scoreboard từ GZCTF: {e}")

        return result




    # ------------------------------------------------------------------
    # Event window (spec event-window §2): GET /api/game/{id} → start/end
    # là EPOCH MILLISECONDS; giá trị ≤ 0 hoặc năm < 2000 = chưa đặt lịch.
    # ------------------------------------------------------------------
    def fetch_event_times(self) -> Optional[EventTimes]:
        if self.game_id is None:
            return None
        try:
            resp = self.session.get(f"{self.origin}/api/game/{self.game_id}", timeout=10)
            if resp.status_code != 200:
                return None
            data = resp.json() or {}
            start = normalize_epoch_to_utc(data.get("start"))
            end = normalize_epoch_to_utc(data.get("end"))
            if start is None and end is None:
                return None
            return EventTimes(start_utc=start, end_utc=end, confidence="high",
                              source=f"gzctf:/api/game/{self.game_id}")
        except Exception:
            return None
