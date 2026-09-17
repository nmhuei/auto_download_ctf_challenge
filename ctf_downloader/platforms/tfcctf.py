import re
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename
from .base import (
    BasePlatform,
    Challenge,
    EventTimes,
    SolveAttribution,
    Verdict,
    normalize_epoch_to_utc,
    safe_get,
)
from .registry import register


def probe_tfcctf_api(origin: str, session: Any, info: Any, done: set) -> bool:
    """Probe TFC CTF platform by detecting domain or querying API endpoints."""
    if "tfcctf_api" in done:
        return False
    done.add("tfcctf_api")

    # Domain match
    if "thefewchosen" in origin:
        info.capabilities["scoreboard"] = True
        info.capabilities["container"] = True
        info.add_signal(f"URL khớp domain TFC CTF: {origin}")
        return True

    # Check api endpoint
    try:
        resp = safe_get(session, f"{origin}/challenge", timeout=5)
        if resp is not None and resp.status_code in (200, 401):
            data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
            if isinstance(data, dict) and ("code" in data or "challenges" in data):
                info.capabilities["scoreboard"] = True
                info.capabilities["container"] = True
                info.add_signal(f"GET {origin}/challenge -> response shape TFC CTF (HTTP {resp.status_code})")
                return True
    except Exception:
        pass

    return False


@register(
    "tfcctf",
    label="TFC CTF",
    throttle=2.0,
    html_markers=(
        "tfcctf",
        "thefewchosen",
        "thefewchosen.com",
        "tfcctf2026",
        "api.ctf.thefewchosen.com",
    ),
    probes=(probe_tfcctf_api,),
    supports_container=True,
    supports_scoreboard=True,
)
class TFCCTFPlatform(BasePlatform):
    """
    Adapter cho nền tảng TFC CTF (The Few Chosen - Angular SPA + REST backend).
    Frontend: https://ctf.thefewchosen.com
    Backend API: https://api.ctf.thefewchosen.com
    Challenge Manager: https://challenge-manager.management.ctf.thefewchosen.com
    """

    def __init__(self, base_url: str, session: requests.Session):
        super().__init__(base_url, session)
        self.ctf_info.platform_type = "tfcctf"
        self.ctf_info.title = "TFC_CTF_2026"
        self._last_verdict: Verdict = "unknown"

        parsed = urllib.parse.urlparse(self.base_url)
        if "thefewchosen" in parsed.netloc:
            self.api_url = "https://api.ctf.thefewchosen.com"
            self.cm_url = "https://challenge-manager.management.ctf.thefewchosen.com"
            self.chall_domain = "challs.ctf.thefewchosen.com"
        else:
            self.api_url = self.base_url
            self.cm_url = self.base_url
            self.chall_domain = parsed.netloc

        auth_hdr = self.session.headers.get("Authorization", "")
        self.token = ""
        if auth_hdr:
            if auth_hdr.lower().startswith("bearer "):
                self.token = auth_hdr[7:].strip()
            elif auth_hdr.lower().startswith("token "):
                self.token = auth_hdr[6:].strip()
            else:
                self.token = auth_hdr.strip()

    @property
    def auth_headers(self) -> Dict[str, str]:
        if not self.token:
            auth_hdr = self.session.headers.get("Authorization", "")
            if auth_hdr:
                if auth_hdr.lower().startswith("bearer "):
                    self.token = auth_hdr[7:].strip()
                else:
                    self.token = auth_hdr.strip()
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    @property
    def last_verdict(self) -> Verdict:
        return self._last_verdict

    @last_verdict.setter
    def last_verdict(self, value: Verdict) -> None:
        self._last_verdict = value

    def authenticate(self) -> bool:
        """
        Kiểm tra xác thực bằng token Authorization: Bearer <token>.
        """
        if not self.token:
            Logger.warning("Chưa cung cấp token xác thực cho TFC CTF.")
            return False

        headers = self.auth_headers

        # Thử lấy thông tin team
        try:
            resp = self.session.get(f"{self.api_url}/team", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                team_name = data.get("team_name") or data.get("name") or data.get("username")
                if team_name:
                    self.ctf_info.team_name = str(team_name)
                    self.ctf_info.user_name = str(team_name)
                Logger.success(f"Xác thực thành công TFC CTF! Team: {team_name or 'OK'}")
                return True
            elif resp.status_code == 401:
                err_data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
                err_code = err_data.get("code") or err_data.get("error")
                if err_code == "expiredsignature":
                    Logger.error("❌ Token đã HẾT HẠN (expiredsignature). Vui lòng lấy accessToken mới hoặc refreshToken.")
                else:
                    Logger.error(f"❌ Xác thực không hợp lệ (HTTP 401: {err_code or resp.text})")
                return False
        except Exception as exc:
            Logger.warning(f"Không truy vấn được /team: {exc}")

        # Thử query /challenge nếu /team thất bại
        try:
            resp = self.session.get(f"{self.api_url}/challenge", headers=headers, timeout=10)
            if resp.status_code == 200:
                Logger.success("Xác thực thành công TFC CTF qua /challenge!")
                return True
            elif resp.status_code == 401:
                err_data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
                err_code = err_data.get("code") or err_data.get("error")
                if err_code == "expiredsignature":
                    Logger.error("❌ Token đã HẾT HẠN (expiredsignature). Vui lòng lấy accessToken mới từ DevTools.")
                else:
                    Logger.error(f"❌ Xác thực không hợp lệ (HTTP 401: {err_code or resp.text})")
                return False
        except Exception as exc:
            Logger.error(f"Lỗi khi kiểm tra xác thực TFC CTF: {exc}")

        return False

    def fetch_event_times(self) -> Optional[EventTimes]:
        """Thời gian bắt đầu giải đấu từ frontend script (1788602400)."""
        return EventTimes(
            start_utc=normalize_epoch_to_utc(1788602400),
            end_utc=normalize_epoch_to_utc(1788602400 + 86400),
            confidence="high",
            source="tfcctf:eventStartUnix",
        )

    def fetch_challenges(self) -> List[Challenge]:
        """Tải toàn bộ challenges, category, difficulty và file attachments."""
        try:
            resp = self.session.get(f"{self.api_url}/challenge", headers=self.auth_headers, timeout=15)
            if resp.status_code != 200:
                err_text = resp.text[:200]
                Logger.error(f"Không tải được challenges từ {self.api_url}/challenge (HTTP {resp.status_code}: {err_text})")
                return []

            try:
                payload = resp.json()
            except Exception:
                payload = None

            if isinstance(payload, list):
                raw_challs = payload
                raw_cats = []
                raw_diffs = []
            elif isinstance(payload, dict):
                body = payload.get("data")
                if isinstance(body, dict):
                    raw_challs = body.get("challenges", [])
                    raw_cats = body.get("categories", [])
                    raw_diffs = body.get("difficulties", [])
                elif isinstance(body, list):
                    raw_challs = body
                    raw_cats = payload.get("categories", [])
                    raw_diffs = payload.get("difficulties", [])
                else:
                    raw_challs = payload.get("challenges", [])
                    raw_cats = payload.get("categories", [])
                    raw_diffs = payload.get("difficulties", [])
            else:
                raw_challs, raw_cats, raw_diffs = [], [], []

            if not isinstance(raw_challs, list):
                raw_challs = []
            if not isinstance(raw_cats, list):
                raw_cats = []
            if not isinstance(raw_diffs, list):
                raw_diffs = []

            cat_map = {c["id"]: c.get("name", "Misc") for c in raw_cats if isinstance(c, dict) and "id" in c}
            diff_map = {d["id"]: d.get("name", "") for d in raw_diffs if isinstance(d, dict) and "id" in d}

            challenges: List[Challenge] = []
            for item in raw_challs:
                if not isinstance(item, dict):
                    continue
                chall_id = item.get("challenge_id") or item.get("id")
                name = item.get("challenge_name") or item.get("name") or f"Challenge_{chall_id}"
                cat_id = item.get("category_id")
                category = cat_map.get(cat_id, "Misc") or "Misc"
                diff_id = item.get("difficulty_id")
                difficulty = diff_map.get(diff_id, "")

                description = item.get("description", "")
                author = item.get("challenge_author") or item.get("author")
                solves = item.get("amount_solves", 0)

                flags_data = item.get("flags", [])
                points = 0
                is_solved = False
                if flags_data and isinstance(flags_data, list):
                    points = sum(f.get("flag_points", 0) for f in flags_data if isinstance(f, dict))
                    is_solved = all(f.get("is_solved", False) for f in flags_data if isinstance(f, dict))
                else:
                    points = item.get("points", 0)
                    is_solved = item.get("is_solved", False)

                tags = []
                if difficulty:
                    tags.append(difficulty)
                if item.get("is_dynamic") or item.get("connection_mode") == "instance":
                    tags.append("container")
                if item.get("connection_type"):
                    tags.append(item.get("connection_type"))

                # File attachments
                files_list: List[Tuple[str, str]] = []
                for f in item.get("files", []):
                    if isinstance(f, dict):
                        f_url = f.get("file_url") or f.get("url")
                        f_name = f.get("file_name") or f.get("name") or (f_url.split("/")[-1] if f_url else "attachment")
                        if f_url:
                            files_list.append((self.get_full_file_url(f_url), sanitize_filename(f_name)))
                    elif isinstance(f, str):
                        files_list.append((self.get_full_file_url(f), sanitize_filename(f.split("/")[-1])))

                chall_obj = Challenge(
                    id=chall_id,
                    name=name,
                    category=category.strip().capitalize(),
                    points=points,
                    description=description,
                    author=author,
                    tags=tags,
                    hints=[],
                    files=files_list,
                    solved_by_me=is_solved,
                    solves_count=solves,
                    raw_data=item,
                )
                challenges.append(chall_obj)

            self.ctf_info.challenges = challenges
            Logger.info(f"Đã tải {len(challenges)} challenges từ TFC CTF.")
            return challenges

        except Exception as exc:
            Logger.error(f"Lỗi khi tải challenges TFC CTF: {exc}")
            return []

    def get_full_file_url(self, file_path: str) -> str:
        if not file_path:
            return ""
        if file_path.startswith("http://") or file_path.startswith("https://"):
            return file_path
        return urllib.parse.urljoin(self.api_url, file_path)

    def fetch_solve_attribution(self, challenge_ids: List[Any]) -> Dict[Any, SolveAttribution]:
        """Lấy thông tin challenge đã được giải từ TFC CTF."""
        wanted = {str(c): c for c in (challenge_ids or [])}
        result: Dict[Any, SolveAttribution] = {}
        try:
            resp = self.session.get(f"{self.api_url}/challenge", headers=self.auth_headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                raw_challs = data.get("challenges", [])
                if not raw_challs and isinstance(data.get("data"), dict):
                    raw_challs = data["data"].get("challenges", [])
                for item in raw_challs:
                    cid = str(item.get("challenge_id") or item.get("id"))
                    if cid in wanted:
                        flags_data = item.get("flags", [])
                        is_solved = False
                        if flags_data and isinstance(flags_data, list):
                            is_solved = any(f.get("is_solved", False) for f in flags_data if isinstance(f, dict))
                        else:
                            is_solved = bool(item.get("is_solved", False))
                        if is_solved:
                            orig_id = wanted[cid]
                            result[orig_id] = SolveAttribution(by_me=True, by_team=True)
        except Exception as exc:
            Logger.warning(f"Lỗi khi lấy solve attribution từ TFC CTF: {exc}")
        return result

    def submit_flag(self, challenge_id: Any, flag: str) -> Tuple[bool, str]:
        """
        Nộp flag lên /challenge/submit.
        """
        url = f"{self.api_url}/challenge/submit"
        payload = {
            "challenge_id": challenge_id,
            "flag": flag.strip(),
        }

        try:
            resp = self.session.post(url, json=payload, headers=self.auth_headers, timeout=15)
            data = resp.json() if "json" in resp.headers.get("content-type", "") else {}

            if resp.status_code == 200 and data.get("ok"):
                self.last_verdict = "correct"
                return True, "🎉 Flag chính xác! Chúc mừng bạn đã giải quyết challenge!"
            elif resp.status_code == 429:
                self.last_verdict = "ratelimited"
                return False, "⏳ Rate limit: Bạn đang submit quá nhanh, vui lòng thử lại sau."
            elif resp.status_code == 400 or (resp.status_code == 200 and not data.get("ok")):
                self.last_verdict = "incorrect"
                msg = data.get("error") or data.get("message") or "Flag không đúng."
                return False, f"❌ {msg}"
            else:
                self.last_verdict = "unknown"
                return False, f"Máy chủ phản hồi HTTP {resp.status_code}: {data.get('error') or resp.text[:100]}"
        except Exception as exc:
            self.last_verdict = "unknown"
            return False, f"Lỗi kết nối khi nộp flag: {exc}"

    def start_instance(self, challenge_id: Any) -> Tuple[bool, Dict[str, Any]]:
        """Khởi chạy dynamic container qua Challenge Manager."""
        url = f"{self.cm_url}/isolated"
        payload = {"name": challenge_id}
        try:
            resp = self.session.post(url, json=payload, headers=self.auth_headers, timeout=20)
            if resp.status_code in (200, 201):
                data = resp.json() if "json" in resp.headers.get("content-type", "") else {}
                dep_name = data.get("name") or data.get("deploymentName") or str(challenge_id)
                entry = f"{dep_name}.{self.chall_domain}"
                return True, {
                    "entry": entry,
                    "time_left": data.get("expiry"),
                    "message": f"Container đã khởi động: {entry}",
                    "raw": data,
                }
            return False, {"message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        except Exception as exc:
            return False, {"message": str(exc)}

    def stop_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        """Dừng dynamic container."""
        url = f"{self.cm_url}/isolated/{challenge_id}"
        try:
            resp = self.session.delete(url, headers=self.auth_headers, timeout=15)
            if resp.status_code in (200, 204):
                return True, "Container đã dừng."
            return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
        except Exception as exc:
            return False, str(exc)

    def fetch_scoreboard(self, if_none_match: Optional[str] = None) -> Dict[str, Any]:
        """Tải bảng xếp hạng từ /leaderboard."""
        url = f"{self.api_url}/leaderboard"
        result: Dict[str, Any] = {
            "title": "TFC CTF Scoreboard",
            "my_team": self.ctf_info.team_name,
            "my_user": self.ctf_info.user_name,
            "my_rank": None,
            "my_score": None,
            "total_teams": 0,
            "standings": [],
        }
        try:
            resp = self.session.get(url, params={"scope": "all"}, headers=self.auth_headers, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                teams = data if isinstance(data, list) else data.get("leaderboard", [])
                standings = []
                for idx, t in enumerate(teams, 1):
                    t_name = t.get("team_name") or t.get("name")
                    score = t.get("score") or t.get("points", 0)
                    standings.append({
                        "pos": idx,
                        "team": t_name,
                        "score": score,
                    })
                    if t_name and t_name == self.ctf_info.team_name:
                        result["my_rank"] = idx
                        result["my_score"] = score
                result["standings"] = standings
                result["total_teams"] = len(standings)
        except Exception:
            pass
        return result
