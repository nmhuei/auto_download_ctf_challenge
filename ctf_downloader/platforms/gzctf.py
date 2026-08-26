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
from ..utils.logger import Logger
from .registry import register

# Bộ field đặc trưng của GZCTF ClientConfig (GET /api/config)
_GZCTF_CONFIG_FIELDS = ("Title", "Slogan", "PortMapping", "DefaultLifetime")


def probe_api_config(origin: str, session, info, done: set) -> bool:
    """/api/config (ClientConfig): xác nhận GZCTF + làm giàu capabilities."""
    if "gzctf_config" in done:
        return False
    done.add("gzctf_config")
    data, status = safe_get_json(session, f"{origin}/api/config")
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


def solve_hash_pow(challenge_id: str, difficulty: int,
                   max_iter: int = 50_000_000) -> Optional[str]:
    """Giải HashPow của GZCTF đúng wire-format upstream:

    - ``challenge_id``: hex string từ PowChallenge (vd 12-hex = 6 bytes).
    - Preimage server hash: ``SHA256(unhexlify(challenge) + answer_bytes)``
      (KHÔNG phải ASCII string).
    - Answer ĐÚNG ``AnswerLength * 2`` = 16 hex (8 bytes).

    Trả answer dạng 16-hex lowercase, hoặc None nếu vượt ``max_iter``.
    """
    if difficulty <= 0:
        return ""
    try:
        prefix = bytes.fromhex(challenge_id)
    except ValueError:
        prefix = challenge_id.encode()  # fallback: id không phải hex
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

    # 1. ClientConfig (/api/config): provider/site-key bật -> dừng sạch.
    cfg, _status = safe_get_json(sess, f"{origin}/api/config", statuses=(200,))
    if isinstance(cfg, dict):
        provider = str(cfg.get("CaptchaProvider") or "").strip().lower()
        site_key_on = any(str(cfg.get(k) or "").strip()
                          for k in _CAPTCHA_SITEKEY_FIELDS)
        if provider and provider not in ("none", "hashpow", "pow"):
            raise PlatformRegisterUnsupported(
                "⚠️ Platform bật captcha "
                f"({cfg.get('CaptchaProvider')}) — đăng ký thủ công tại "
                f"{origin}/register. Tool không bypass captcha.")
        if site_key_on:
            raise PlatformRegisterUnsupported(
                "⚠️ Platform cấu hình site-key captcha (Turnstile/reCAPTCHA/"
                "hCaptcha) — đăng ký thủ công. Tool không bypass captcha.")

    # 2. GET /api/captcha: 404/204/rỗng (GZCTF cũ) HOẶC 200 {"type":"None",
    #    "siteKey":""} (GZCTF hiện đại) -> không cần captcha, đi tiếp.
    try:
        resp = sess.get(f"{origin}/api/captcha", timeout=15)
    except Exception as exc:
        Logger.warning(f"GZCTF register: GET /api/captcha lỗi ({exc}) — "
                       "tiếp tục như không có captcha.")
        return {}
    if resp.status_code in (404, 204):
        return {}
    try:
        data = resp.json()
    except Exception:
        return {}
    if not isinstance(data, dict) or not data:
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
        chal_url = f"{origin}/api/captcha/PowChallenge"
        chal_resp = sess.get(chal_url, timeout=15)
        if chal_resp.status_code != 200:
            raise PlatformRegisterUnsupported(
                f"Lấy PowChallenge thất bại (HTTP {chal_resp.status_code}).")
        chal = chal_resp.json() or {}
        chal_id = str(chal.get("id") or chal.get("challenge") or "")
        if not chal_id:
            raise PlatformRegisterUnsupported(
                f"PowChallenge thiếu id/challenge: {chal}")
        raw_diff = chal.get("difficulty")
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
        return {"challenge_id": chal_id,
                "difficulty": difficulty}

    # Loại captcha khác/không rõ -> an toàn là dừng
    raise PlatformRegisterUnsupported(
        f"⚠️ Platform yêu cầu captcha không nhận diện được ({data}) — "
        "đăng ký thủ công. Tool không bypass captcha.")


def gzctf_register(platform, *, username: str, email: str, password: str,
                   verify_email_hook=None) -> Dict[str, Any]:
    """Flow auto-register GZCTF (spec auto-register §2)."""
    origin, sess = platform.origin, platform.session

    pow_task = gzctf_probe_captcha(platform)
    payload: Dict[str, Any] = {"userName": username, "email": email,
                               "password": password}
    if pow_task:
        solution = solve_hash_pow(pow_task["challenge_id"],
                                  pow_task["difficulty"])
        if solution is None:
            return {"ok": False,
                    "message": "Không giải được HashPow trong giới hạn vòng lặp."}
        # Wire-format upstream: server bind ModelWithCaptcha.Challenge và split
        # theo ':' -> ticket = "<challenge_ID>:<answer 16-hex>", field JSON
        # tên "challenge".
        payload["challenge"] = f"{pow_task['challenge_id']}:{solution}"
        Logger.info("GZCTF: đã giải HashPow (PoW) — tiếp tục register.")

    try:
        resp = sess.post(f"{origin}/api/account/register", json=payload,
                         timeout=20)
    except Exception as exc:
        return {"ok": False, "message": f"Lỗi mạng khi register: {exc}"}

    if resp.status_code != 200:
        detail = (resp.text or "").strip().strip('"')[:200]
        return {"ok": False,
                "message": f"Register thất bại (HTTP {resp.status_code}): {detail}"}

    Logger.success("GZCTF: register OK (HTTP 200) — tiến hành login lấy token.")
    result: Dict[str, Any] = {"ok": True, "message": "Đã register"}

    if verify_email_hook is not None:
        verified = verify_email_hook(sess)
        result["email_verified"] = bool(verified)

    # Login NGAY để lấy GZCTF_Token (cookie hoặc body trả JWT).
    # Ghi chú upstream: LoginModel cũng kế thừa captcha — platform BẬT captcha
    # thì bước login cần challenge RIÊNG (single-use, không tái dùng ticket
    # register); chỉ warning, xử lý đầy đủ ở đợt sau.
    if pow_task:
        Logger.warning(
            "GZCTF: platform dùng HashPow — login-sau-register có thể bị đòi "
            "captcha riêng; nếu vậy hãy đăng nhập thủ công bằng credentials "
            "đã in.")
    try:
        login = sess.post(f"{origin}/api/account/login",
                          json={"name": username, "password": password},
                          timeout=20)
        if login.status_code == 200:
            token = (login.text or "").strip().strip('"')
            cookies = {c.name: c.value for c in sess.cookies}
            gz_token = cookies.get("GZCTF_Token") or (token or None)
            if gz_token:
                result["token"] = gz_token
                result["cookies"] = cookies
        else:
            Logger.warning(
                f"GZCTF: login sau register thất bại (HTTP {login.status_code}) "
                "— dùng credentials vừa in để đăng nhập thủ công.")
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
                    single_data = {}

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
        payload = {"flag": flag.strip()}

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

                    time.sleep(self.SUBMISSION_POLL_INTERVAL)

                self.last_verdict = "unknown"
                return False, (
                    f"⚠️ Không xác định được kết quả chấm (Submission ID: {sub_id}). "
                    f"Hãy kiểm tra trang submissions của giải."
                )

            elif resp.status_code == 400:
                err_text = resp.text.strip().strip('"') or "Invalid Flag"
                self.last_verdict = "incorrect"
                return False, f"❌ Flag không đúng ({err_text})."
            elif resp.status_code == 403:
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
        except Exception:
            pass
        return {"status": "unknown", "entry": None, "close_time": None}

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

    def fetch_scoreboard(self) -> Dict[str, Any]:
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
            "standings": []
        }

        if not self.game_id:
            return result

        url = f"{self.origin}/api/game/{self.game_id}/scoreboard"
        try:
            resp = self.session.get(url, timeout=15)
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
